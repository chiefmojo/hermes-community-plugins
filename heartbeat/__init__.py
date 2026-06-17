"""Heartbeat Plugin — proactive scheduled agent runner with state tracking.

Wakes a lightweight LLM on a configurable schedule via a SEPARATE subprocess
(not the main session). Gives it HEARTBEAT.md as its instructions (the prompt
IS the program), full access to all agent tools via the hermes CLI, and only
notifies the user when something needs attention.

Execution model:
  - Spawns `hermes chat -q "prompt" -m cheap_model -Q --yolo --accept-hooks`
  - Runs in complete isolation — does NOT touch the main agent's session
  - Model override is at the process level (cheap model actually used)
  - Output captured, parsed for NOTHING_TO_FLAG vs findings
  - Findings delivered to user's chat via async injection (same as async-delegate)

Hooks:
  pre_gateway_dispatch  — capture GatewayRunner + event loop, start scheduler

Tools:
  heartbeat_create  — create a heartbeat with name, schedule, model, etc.
  heartbeat_list    — list all heartbeats with status
  heartbeat_run     — manually trigger a heartbeat immediately
"""

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults (overridable from plugin.yaml config block)
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL = 1800       # 30 minutes
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_PROMPT_FILE = str(Path.home() / ".hermes" / "HEARTBEAT.md")
DEFAULT_STATE_DB = str(Path.home() / ".hermes" / "data" / "heartbeat.db")
DEFAULT_QUIET_START = "23:00"
DEFAULT_QUIET_END = "07:00"
DEFAULT_TZ = "America/New_York"
STATE_TTL_HOURS = 48
LOG_RETENTION_DAYS = 30
SCHEDULER_POLL_SECS = 10
HEARTBEAT_TIMEOUT_SECS = 300  # 5 minutes max per heartbeat run
MAX_OUTPUT_CHARS = 16000

# Default toolsets for heartbeat subagents
_HB_DEFAULT_TOOLSETS = "web,terminal,file,search"

# Temp dir for heartbeat run files
HB_RUNS_DIR = Path.home() / ".hermes" / "heartbeat-runs"

# ---------------------------------------------------------------------------
# Module-level state — populated by pre_gateway_dispatch hook
# ---------------------------------------------------------------------------

_gateway_runner = None
_gateway_loop = None
_latest_routing: Optional[dict] = None
_scheduler_thread: Optional[threading.Thread] = None
_scheduler_stop = threading.Event()

# Cross-process scheduler lock — ensures only ONE process (the first to load
# the plugin) runs the heartbeat scheduler. Prevents WebUI sessions, CLI
# sessions, and gateway from each starting their own scheduler and spawning
# duplicate subprocesses.
_scheduler_lock_fd = None

def _try_acquire_scheduler_lock() -> bool:
    """Try to acquire an exclusive flock. Returns True if acquired."""
    global _scheduler_lock_fd
    try:
        lock_path = Path.home() / ".hermes" / "data" / "heartbeat.scheduler.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        _scheduler_lock_fd = open(lock_path, "w")
        import fcntl
        fcntl.flock(_scheduler_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (IOError, OSError):
        if _scheduler_lock_fd:
            _scheduler_lock_fd.close()
            _scheduler_lock_fd = None
        return False

# Per-heartbeat run locks: name -> Lock (prevents overlapping runs of same heartbeat)
_run_locks: Dict[str, threading.Lock] = {}
_run_locks_lock = threading.Lock()  # guards _run_locks itself

# Global concurrency semaphore — limits total simultaneous heartbeat subprocesses
# across ALL heartbeats. Each subprocess loads ~500MB (full Hermes agent), so
# running more than 1 at a time risks OOM on small hosts.
_hb_concurrency = threading.Semaphore(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_db_path = DEFAULT_STATE_DB


def _find_hermes() -> str:
    """Locate the hermes executable."""
    hermes = shutil.which("hermes")
    if hermes:
        return hermes
    for candidate in [
        "/root/.local/bin/hermes",
        "/usr/local/bin/hermes",
        os.path.expanduser("~/.local/bin/hermes"),
    ]:
        if Path(candidate).exists():
            return candidate
    return "hermes"  # last resort


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode."""
    db = sqlite3.connect(_db_path, timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    return db


def _init_db() -> None:
    """Create tables if they don't exist."""
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    db = _get_db()
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS heartbeats (
                name TEXT PRIMARY KEY,
                schedule TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                prompt_file TEXT,
                deliver TEXT DEFAULT 'origin',
                model TEXT DEFAULT 'deepseek/deepseek-v4-flash',
                enabled INTEGER DEFAULT 1,
                quiet_hours_start TEXT DEFAULT '23:00',
                quiet_hours_end TEXT DEFAULT '07:00',
                timezone TEXT DEFAULT 'America/New_York',
                created_at REAL,
                last_run_at REAL,
                next_run_at REAL,
                run_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS heartbeat_state (
                heartbeat_name TEXT,
                state_key TEXT,
                state_value TEXT,
                flagged_at REAL,
                PRIMARY KEY (heartbeat_name, state_key)
            );

            CREATE TABLE IF NOT EXISTS heartbeat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                heartbeat_name TEXT,
                run_at REAL,
                result TEXT,
                summary TEXT,
                duration_ms INTEGER
            );
        """)
        db.commit()
    finally:
        db.close()


def _parse_schedule(schedule: str) -> int:
    """Parse schedule strings like 'every 30m', 'every 1h', 'every 6h' into seconds."""
    match = re.match(r"every\s+(\d+)\s*(m|h|min|mins|hour|hours)", schedule.strip().lower())
    if not match:
        raise ValueError(
            f"Invalid schedule format: '{schedule}'. "
            "Use formats like 'every 30m', 'every 1h', 'every 6h'."
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("h"):
        return amount * 3600
    else:
        return amount * 60


def _is_quiet_hours(hb: dict) -> bool:
    """Check if current time falls within the heartbeat's quiet hours."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(hb.get("timezone", DEFAULT_TZ))
        now = datetime.now(tz)

        start_str = hb.get("quiet_hours_start", DEFAULT_QUIET_START)
        end_str = hb.get("quiet_hours_end", DEFAULT_QUIET_END)

        start_h, start_m = map(int, start_str.split(":"))
        end_h, end_m = map(int, end_str.split(":"))

        start_mins = start_h * 60 + start_m
        end_mins = end_h * 60 + end_m
        now_mins = now.hour * 60 + now.minute

        if start_mins <= end_mins:
            # Normal range, e.g. 08:00-22:00
            return start_mins <= now_mins < end_mins
        else:
            # Wraps midnight, e.g. 23:00-07:00
            return now_mins >= start_mins or now_mins < end_mins
    except Exception as e:
        logger.warning("heartbeat: quiet hours check failed: %s", e)
        return False


def _get_state_summary(heartbeat_name: str) -> str:
    """Get a human-readable summary of previous state for the injected prompt."""
    db = _get_db()
    try:
        cutoff = time.time() - (STATE_TTL_HOURS * 3600)
        rows = db.execute(
            "SELECT state_key, state_value, flagged_at FROM heartbeat_state "
            "WHERE heartbeat_name = ? AND flagged_at > ? ORDER BY flagged_at DESC",
            (heartbeat_name, cutoff),
        ).fetchall()

        if not rows:
            return "No previous findings."

        lines = []
        for key, value, flagged_at in rows:
            hours_ago = (time.time() - flagged_at) / 3600
            if hours_ago < 1:
                ago = f"{int(hours_ago * 60)}m ago"
            else:
                ago = f"{hours_ago:.1f}h ago"
            lines.append(f"- {key}: {value} — flagged {ago}")

        return "\n".join(lines)
    except Exception as e:
        logger.warning("heartbeat: state summary failed: %s", e)
        return "No previous findings."
    finally:
        db.close()


def _cleanup_old_state() -> None:
    """Remove expired state entries and old log entries."""
    db = _get_db()
    try:
        cutoff = time.time() - (STATE_TTL_HOURS * 3600)
        db.execute("DELETE FROM heartbeat_state WHERE flagged_at < ?", (cutoff,))
        log_cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
        db.execute("DELETE FROM heartbeat_log WHERE run_at < ?", (log_cutoff,))
        db.commit()
    except Exception as e:
        logger.warning("heartbeat: state cleanup failed: %s", e)
    finally:
        db.close()


def _get_run_lock(name: str) -> threading.Lock:
    """Get or create a per-heartbeat run lock."""
    with _run_locks_lock:
        if name not in _run_locks:
            _run_locks[name] = threading.Lock()
        return _run_locks[name]


def _log_run(name: str, start_time: float, result: str, summary: str,
             elapsed_ms: int = 0) -> None:
    """Insert a log entry for a heartbeat run."""
    try:
        db = _get_db()
        db.execute(
            "INSERT INTO heartbeat_log (heartbeat_name, run_at, result, summary, duration_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, time.time(), result, summary[:500], elapsed_ms),
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("heartbeat: failed to log run: %s", e)


def _save_findings_state(name: str, summary: str) -> None:
    """Save heartbeat findings to state for next run's reference."""
    try:
        db = _get_db()
        db.execute(
            "INSERT OR REPLACE INTO heartbeat_state (heartbeat_name, state_key, state_value, flagged_at) "
            "VALUES (?, ?, ?, ?)",
            (name, f"findings:{int(time.time())}", summary[:300], time.time()),
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("heartbeat: failed to save state: %s", e)


def _update_schedule(hb: dict) -> None:
    """Update last_run_at and next_run_at in SQLite."""
    try:
        db = _get_db()
        now = time.time()
        interval = hb.get("interval_seconds", DEFAULT_INTERVAL)
        db.execute(
            "UPDATE heartbeats SET last_run_at = ?, next_run_at = ?, run_count = run_count + 1 "
            "WHERE name = ?",
            (now, now + interval, hb["name"]),
        )
        db.commit()
        db.close()
    except Exception as e:
        logger.warning("heartbeat: failed to update schedule: %s", e)


# ---------------------------------------------------------------------------
# Heartbeat execution — subprocess-based
# ---------------------------------------------------------------------------


def _execute_heartbeat(hb: dict) -> None:
    """Run a single heartbeat: spawn a subprocess agent with cheap model.

    Steps:
      1. Read HEARTBEAT.md (or custom prompt file)
      2. Build prompt with previous state summary + rules
      3. Spawn `hermes chat -q "prompt" -m model -Q --yolo --accept-hooks`
      4. Wait for subprocess completion (with timeout)
      5. Parse output: NOTHING_TO_FLAG → silent, otherwise → deliver findings
      6. Log result, update state, update schedule
    """
    global _gateway_runner, _gateway_loop

    # NOTE: _gateway_runner is only needed for findings delivery (_deliver_findings),
    # not for subprocess execution. Don't block the heartbeat run — just log if
    # delivery won't be possible yet (e.g., WebUI-only sessions after restart).
    if not _gateway_runner or not _gateway_loop:
        logger.info("heartbeat: gateway_runner not captured yet — will run but cannot deliver findings until a platform message arrives")

    name = hb["name"]
    lock = _get_run_lock(name)

    if not lock.acquire(blocking=False):
        logger.info("heartbeat: '%s' already running, skipping", name)
        return

    # Acquire global concurrency semaphore — ensures only 1 heartbeat subprocess
    # runs at a time across ALL heartbeats (each loads ~500MB of Hermes agent)
    if not _hb_concurrency.acquire(blocking=False):
        logger.info("heartbeat: '%s' deferred — another heartbeat subprocess is running", name)
        lock.release()
        return

    start_time = time.time()
    run_id = f"hb_{name}_{int(start_time)}"

    # File paths for this run
    HB_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    prompt_file = HB_RUNS_DIR / f"{run_id}.prompt"
    out_file = HB_RUNS_DIR / f"{run_id}.output"
    err_file = HB_RUNS_DIR / f"{run_id}.err"
    done_file = HB_RUNS_DIR / f"{run_id}.done"

    try:
        logger.info("heartbeat: executing '%s' (subprocess mode)", name)

        # --- 1. Read prompt file ---
        prompt_path = hb.get("prompt_file") or DEFAULT_PROMPT_FILE
        prompt_path = os.path.expanduser(prompt_path)
        prompt_content = ""
        if os.path.exists(prompt_path):
            with open(prompt_path, "r") as f:
                prompt_content = f.read()
        else:
            prompt_content = (
                "# Heartbeat\n\nNo HEARTBEAT.md found. "
                "Create one at ~/.hermes/HEARTBEAT.md with your check instructions.\n"
                "Respond: NOTHING_TO_FLAG\n"
            )

        # --- 2. Get previous state and build full prompt ---
        state_summary = _get_state_summary(name)
        now_iso = datetime.now(timezone.utc).isoformat()
        model_full = hb.get("model", DEFAULT_MODEL)

        full_prompt = (
            f"[Heartbeat: {name} | {now_iso}]\n\n"
            f"## Previous State\n{state_summary}\n\n"
            f"## Instructions\n{prompt_content}\n\n"
            f"## Rules\n"
            f"- If nothing needs attention, respond with exactly: NOTHING_TO_FLAG\n"
            f"- If something needs attention, respond with a brief summary\n"
            f"- Do not repeat information from previous state\n"
            f"- Be brief — one line per finding\n"
            f"- Only flag NEW information or changes since last check\n"
            f"- When in doubt, NOTHING_TO_FLAG is preferred over noise\n"
        )

        # --- 3. Write prompt and wrapper script ---
        prompt_file.write_text(full_prompt)
        hermes_bin = _find_hermes()
        toolsets = _HB_DEFAULT_TOOLSETS

        # Parse model string: "provider/model" or just "model"
        model_str = hb.get("model", DEFAULT_MODEL)
        provider_arg = ""
        if "/" in model_str:
            provider_name, model_name = model_str.split("/", 1)
            provider_arg = f'--provider "{provider_name}" '
            model_str = model_name

        # Use a wrapper script to avoid shell quoting issues with the prompt.
        wrapper_script = HB_RUNS_DIR / f"{run_id}.sh"
        wrapper_script.write_text(
            f'#!/bin/bash\n'
            f'PROMPT=$(cat "{prompt_file}")\n'
            f'"{hermes_bin}" chat -q "Complete the following task: $PROMPT" -m "{model_str}" {provider_arg}-Q --yolo --accept-hooks '
            f'--source heartbeat --ignore-rules -t "{toolsets}" >"{out_file}" 2>"{err_file}"\n'
            f'echo $? >"{done_file}"\n'
        )
        wrapper_script.chmod(0o755)

        # --- 4. Spawn subprocess ---
        proc = subprocess.Popen(
            ["bash", str(wrapper_script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        logger.info("heartbeat: '%s' spawned subprocess (PID %d, model=%s)",
                     name, proc.pid, model_str)

        # --- 5. Wait for completion with timeout ---
        try:
            proc.wait(timeout=HEARTBEAT_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.warning("heartbeat: '%s' timed out after %ds", name, HEARTBEAT_TIMEOUT_SECS)
            _log_run(name, start_time, "timeout",
                     f"Subprocess timed out after {HEARTBEAT_TIMEOUT_SECS}s", elapsed_ms)
            _update_schedule(hb)
            return

        # --- 6. Read results ---
        exit_code = "-1"
        if done_file.exists():
            try:
                exit_code = done_file.read_text().strip()
            except Exception:
                pass

        output_text = ""
        if out_file.exists():
            try:
                output_text = out_file.read_text()[:MAX_OUTPUT_CHARS]
            except Exception:
                pass

        err_text = ""
        if err_file.exists():
            try:
                err_text = err_file.read_text()[:2000]
            except Exception:
                pass

        elapsed_ms = int((time.time() - start_time) * 1000)

        if exit_code != "0":
            logger.error("heartbeat: '%s' exited with code %s (stderr: %s)",
                         name, exit_code, err_text[:300])
            _log_run(name, start_time, "error",
                     f"Exit code {exit_code}: {err_text[:200]}", elapsed_ms)
            _update_schedule(hb)
            return

        # --- 7. Parse output: NOTHING_TO_FLAG or findings ---
        stripped = output_text.strip()
        if "NOTHING_TO_FLAG" in output_text:
            logger.info("heartbeat: '%s' returned NOTHING_TO_FLAG — staying silent", name)
            _log_run(name, start_time, "nothing_to_flag", "No findings", elapsed_ms)
        elif not stripped or stripped in ("(empty)", "None", "N/A", "null", "-"):
            logger.info("heartbeat: '%s' returned empty/noise output — treating as nothing to flag", name)
            _log_run(name, start_time, "nothing_to_flag", f"Empty/noise output: {stripped[:100]}", elapsed_ms)
        else:
            # Findings found!
            summary = output_text.strip()[:4000]
            logger.info("heartbeat: '%s' has findings — delivering to user", name)
            _log_run(name, start_time, "findings", summary, elapsed_ms)
            _save_findings_state(name, summary)
            _deliver_findings(name, summary)

        # Update schedule
        _update_schedule(hb)

        logger.info("heartbeat: '%s' complete (%dms, exit=%s)", name, elapsed_ms, exit_code)

    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.error("heartbeat: execution failed for '%s': %s", name, e)
        _log_run(name, start_time, "error", str(e)[:500], elapsed_ms)
    finally:
        lock.release()
        _hb_concurrency.release()
        # Clean up temp files (best effort)
        for f in [prompt_file, out_file, err_file, done_file]:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass
        # Clean up wrapper script
        wrapper = HB_RUNS_DIR / f"{run_id}.sh"
        try:
            if wrapper.exists():
                wrapper.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Findings delivery — inject into user's chat
# ---------------------------------------------------------------------------


def _deliver_findings(name: str, findings: str) -> None:
    """Inject heartbeat findings into the user's chat as a notification.

    Uses the same injection pattern as async-delegate: build a synthetic
    MessageEvent (internal=True) and inject via the platform adapter.
    The main agent sees it as a notification and relays to the user.
    """
    global _gateway_runner

    if not _gateway_runner:
        logger.warning("heartbeat: no gateway_runner, cannot deliver findings")
        return

    routing = _latest_routing
    if not routing:
        logger.warning("heartbeat: no routing info, cannot deliver findings")
        return

    platform_str = routing.get("platform", "")
    chat_id = routing.get("chat_id", "")
    thread_id = routing.get("thread_id")
    user_id = routing.get("user_id")

    if not platform_str or not chat_id:
        logger.warning("heartbeat: missing platform/chat_id in routing")
        return

    synth_text = (
        f"[💓 Heartbeat: {name}] The following items need attention:\n\n"
        f"{findings}\n\n"
        f"— Relay these findings to the user concisely. Do not add commentary."
    )

    logger.info("heartbeat: delivering findings for '%s' to %s chat=%s thread=%s",
                name, platform_str, chat_id, thread_id)

    try:
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.config import Platform

        # Resolve Platform enum
        platform_enum = None
        try:
            platform_enum = Platform(platform_str)
        except ValueError:
            for p in Platform:
                if p.value == platform_str:
                    platform_enum = p
                    break
        if not platform_enum:
            logger.error("heartbeat: unknown platform '%s'", platform_str)
            return

        # Build SessionSource
        source = SessionSource(
            platform=platform_enum,
            chat_id=chat_id,
            chat_type=routing.get("chat_type", "group"),
            user_id=user_id,
            user_name="system",
            thread_id=thread_id,
        )

        # Build synthetic MessageEvent (internal=True bypasses auth)
        synth_event = MessageEvent(
            text=synth_text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )

        # Find the adapter for this platform
        adapter = None
        for p, a in _gateway_runner.adapters.items():
            p_val = p.value if hasattr(p, "value") else str(p)
            if p_val == platform_str:
                adapter = a
                break

        if not adapter:
            logger.error("heartbeat: no adapter found for platform '%s'", platform_str)
            return

        loop = _gateway_loop
        if not loop:
            logger.error("heartbeat: no event loop available for delivery")
            return

        # Inject on the gateway event loop (same pattern as async-delegate)
        async def _async_deliver():
            try:
                await adapter.handle_message(synth_event)
                logger.info("heartbeat: delivered findings for '%s' as new turn", name)
            except Exception as e:
                logger.error("heartbeat: delivery failed for '%s': %s", name, e)

        future = asyncio.run_coroutine_threadsafe(_async_deliver(), loop)
        future.result(timeout=15)

        logger.info("heartbeat: delivery complete for '%s'", name)

    except Exception as e:
        logger.error("heartbeat: deliver_findings failed for '%s': %s", name, e)


# ---------------------------------------------------------------------------
# Background scheduler daemon thread
# ---------------------------------------------------------------------------


def _scheduler_loop() -> None:
    """Background thread: check every N seconds if any heartbeat is due.

    Each due heartbeat is launched in its own thread so the scheduler
    is not blocked by long-running subprocess executions.
    """
    logger.info("heartbeat: scheduler thread started")

    last_cleanup = 0.0

    while not _scheduler_stop.is_set():
        try:
            _init_db()  # Ensure DB exists
            db = _get_db()
            now = time.time()

            # Get all enabled heartbeats that are due
            rows = db.execute(
                "SELECT name, schedule, interval_seconds, prompt_file, deliver, model, "
                "enabled, quiet_hours_start, quiet_hours_end, timezone, "
                "last_run_at, next_run_at, run_count "
                "FROM heartbeats WHERE enabled = 1"
            ).fetchall()
            db.close()

            for row in rows:
                hb = {
                    "name": row[0],
                    "schedule": row[1],
                    "interval_seconds": row[2],
                    "prompt_file": row[3],
                    "deliver": row[4],
                    "model": row[5],
                    "enabled": row[6],
                    "quiet_hours_start": row[7],
                    "quiet_hours_end": row[8],
                    "timezone": row[9],
                    "last_run_at": row[10],
                    "next_run_at": row[11],
                    "run_count": row[12],
                }

                next_run = hb.get("next_run_at")
                if next_run is None:
                    # Never run before — schedule for now
                    next_run = now

                if now >= next_run:
                    # Check quiet hours
                    if _is_quiet_hours(hb):
                        # Still in quiet hours — reschedule to after quiet period
                        logger.info("heartbeat: '%s' in quiet hours, rescheduling", hb["name"])
                        interval = hb.get("interval_seconds", DEFAULT_INTERVAL)
                        new_next = now + interval
                        try:
                            db2 = _get_db()
                            db2.execute(
                                "UPDATE heartbeats SET next_run_at = ? WHERE name = ?",
                                (new_next, hb["name"]),
                            )
                            db2.commit()
                            db2.close()
                        except Exception:
                            pass
                        continue

                    # Execute the heartbeat in its own thread (non-blocking)
                    t = threading.Thread(
                        target=_execute_heartbeat,
                        args=(hb,),
                        name=f"heartbeat-run-{hb['name']}",
                        daemon=True,
                    )
                    t.start()

            # Periodic cleanup (every hour)
            if now - last_cleanup > 3600:
                _cleanup_old_state()
                # Also clean up old run files
                _cleanup_run_files()
                last_cleanup = now

        except Exception as e:
            logger.error("heartbeat: scheduler error: %s", e)

        _scheduler_stop.wait(SCHEDULER_POLL_SECS)

    logger.info("heartbeat: scheduler thread stopped")


def _ensure_scheduler() -> None:
    """Start the scheduler daemon thread if not already running."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _init_db()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="heartbeat-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()


def _cleanup_run_files() -> None:
    """Remove old heartbeat run files (older than 1 hour)."""
    try:
        if not HB_RUNS_DIR.exists():
            return
        cutoff = time.time() - 3600
        for f in HB_RUNS_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception as e:
        logger.warning("heartbeat: run file cleanup failed: %s", e)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def heartbeat_create_tool(
    name: str,
    schedule: str,
    prompt_file: str = "",
    deliver: str = "origin",
    model: str = "",
    quiet_hours_start: str = "",
    quiet_hours_end: str = "",
    timezone: str = "",
    enabled: bool = True,
) -> str:
    """Create a new heartbeat configuration."""
    _init_db()

    # Parse schedule
    try:
        interval_seconds = _parse_schedule(schedule)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    if interval_seconds < 60:
        return json.dumps({"error": "Minimum interval is 60 seconds (1 minute)."})

    db = _get_db()
    try:
        # Check if name already exists
        existing = db.execute("SELECT name FROM heartbeats WHERE name = ?", (name,)).fetchone()
        if existing:
            return json.dumps({"error": f"Heartbeat '{name}' already exists. Use a different name or remove it first."})

        now = time.time()
        model = model or DEFAULT_MODEL
        prompt_file = prompt_file or DEFAULT_PROMPT_FILE
        quiet_hours_start = quiet_hours_start or DEFAULT_QUIET_START
        quiet_hours_end = quiet_hours_end or DEFAULT_QUIET_END
        tz = timezone or DEFAULT_TZ

        db.execute(
            "INSERT INTO heartbeats "
            "(name, schedule, interval_seconds, prompt_file, deliver, model, enabled, "
            "quiet_hours_start, quiet_hours_end, timezone, created_at, last_run_at, next_run_at, run_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0)",
            (name, schedule, interval_seconds, prompt_file, deliver, model,
             1 if enabled else 0, quiet_hours_start, quiet_hours_end, tz, now, now),
        )
        db.commit()

        logger.info("heartbeat: created '%s' (every %ds)", name, interval_seconds)
        return json.dumps({
            "status": "created",
            "name": name,
            "schedule": schedule,
            "interval_seconds": interval_seconds,
            "model": model,
            "prompt_file": prompt_file,
            "enabled": enabled,
            "message": f"Heartbeat '{name}' created. Will run every {interval_seconds // 60}m.",
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Failed to create heartbeat: {e}"})
    finally:
        db.close()


def heartbeat_list_tool() -> str:
    """List all heartbeats with their status."""
    _init_db()
    db = _get_db()
    try:
        rows = db.execute(
            "SELECT name, schedule, interval_seconds, model, enabled, "
            "last_run_at, next_run_at, run_count, prompt_file "
            "FROM heartbeats ORDER BY created_at"
        ).fetchall()

        if not rows:
            return json.dumps({"heartbeats": [], "message": "No heartbeats configured. Use heartbeat_create to add one."})

        heartbeats = []
        now = time.time()
        for row in rows:
            name, schedule, interval, model, enabled, last_run, next_run, run_count, prompt_file = row

            # Format times
            last_run_str = "never"
            if last_run:
                mins_ago = int((now - last_run) / 60)
                if mins_ago < 60:
                    last_run_str = f"{mins_ago}m ago"
                else:
                    last_run_str = f"{mins_ago // 60}h {mins_ago % 60}m ago"

            next_run_str = "now"
            if next_run and next_run > now:
                secs_until = int(next_run - now)
                if secs_until < 60:
                    next_run_str = f"{secs_until}s"
                elif secs_until < 3600:
                    next_run_str = f"{secs_until // 60}m"
                else:
                    next_run_str = f"{secs_until // 3600}h {(secs_until % 3600) // 60}m"

            heartbeats.append({
                "name": name,
                "schedule": schedule,
                "interval_minutes": interval // 60,
                "model": model,
                "enabled": bool(enabled),
                "last_run": last_run_str,
                "next_run": next_run_str,
                "run_count": run_count,
                "prompt_file": prompt_file,
            })

        return json.dumps({"heartbeats": heartbeats, "count": len(heartbeats)}, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Failed to list heartbeats: {e}"})
    finally:
        db.close()


def heartbeat_run_tool(name: str) -> str:
    """Manually trigger a heartbeat run immediately, ignoring schedule and quiet hours."""
    _init_db()
    db = _get_db()
    try:
        row = db.execute(
            "SELECT name, schedule, interval_seconds, prompt_file, deliver, model, "
            "enabled, quiet_hours_start, quiet_hours_end, timezone, "
            "last_run_at, next_run_at, run_count "
            "FROM heartbeats WHERE name = ?",
            (name,),
        ).fetchone()

        if not row:
            return json.dumps({"error": f"Heartbeat '{name}' not found."})

        hb = {
            "name": row[0],
            "schedule": row[1],
            "interval_seconds": row[2],
            "prompt_file": row[3],
            "deliver": row[4],
            "model": row[5],
            "enabled": row[6],
            "quiet_hours_start": row[7],
            "quiet_hours_end": row[8],
            "timezone": row[9],
            "last_run_at": row[10],
            "next_run_at": row[11],
            "run_count": row[12],
        }

        # Check gateway availability
        if not _gateway_runner:
            return json.dumps({"error": "Gateway not available yet. Send a message first to initialize."})

        # Execute in a separate thread so we don't block the tool response
        def _run():
            _execute_heartbeat(hb)

        t = threading.Thread(target=_run, name=f"heartbeat-run-{name}", daemon=True)
        t.start()

        return json.dumps({
            "status": "triggered",
            "name": name,
            "model": hb.get("model", DEFAULT_MODEL),
            "message": f"Heartbeat '{name}' triggered. Running in background with model {hb.get('model', DEFAULT_MODEL)}.",
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to trigger heartbeat: {e}"})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Hook: pre_gateway_dispatch — capture gateway runner + start scheduler
# ---------------------------------------------------------------------------


def capture_gateway(**kwargs):
    """Capture GatewayRunner, event loop, and session routing from every dispatch.

    Same pattern as async-delegate: grab gateway_runner once, store routing
    for message injection, and start the background scheduler daemon thread.
    """
    global _gateway_runner, _gateway_loop

    gateway = kwargs.get("gateway")

    # Capture the gateway runner and event loop (first time only)
    if gateway and not _gateway_runner:
        _gateway_runner = gateway
        try:
            import asyncio as _asyncio
            _gateway_loop = _asyncio.get_running_loop()
            logger.info("heartbeat: captured GatewayRunner + event loop")
        except RuntimeError:
            try:
                _gateway_loop = _asyncio.get_event_loop()
            except Exception:
                pass
            logger.info("heartbeat: captured GatewayRunner (loop via fallback)")
        _ensure_scheduler()

    # Capture routing info from current message
    event = kwargs.get("event")
    if not event:
        return None

    source = getattr(event, "source", None)
    if not source:
        return None

    routing = {
        "platform": source.platform.value if hasattr(source.platform, "value") else str(source.platform),
        "chat_id": source.chat_id or "",
        "chat_type": source.chat_type or "dm",
        "thread_id": source.thread_id,
        "user_id": source.user_id,
        "user_name": source.user_name,
    }

    # Try to build session_key from source
    try:
        from gateway.session import build_session_key
        routing["session_key"] = build_session_key(source)
    except Exception:
        pass

    global _latest_routing
    _latest_routing = routing

    return None  # Don't modify the event


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register heartbeat plugin tools and hooks."""
    _init_db()

    # -- heartbeat_create --
    ctx.register_tool(
        name="heartbeat_create",
        handler=lambda args, **kw: heartbeat_create_tool(
            name=args.get("name", ""),
            schedule=args.get("schedule", ""),
            prompt_file=args.get("prompt_file", ""),
            deliver=args.get("deliver", "origin"),
            model=args.get("model", ""),
            quiet_hours_start=args.get("quiet_hours_start", ""),
            quiet_hours_end=args.get("quiet_hours_end", ""),
            timezone=args.get("timezone", ""),
            enabled=args.get("enabled", True),
        ),
        schema={
            "name": "heartbeat_create",
            "description": (
                "Create a recurring heartbeat that wakes a lightweight agent on a schedule. "
                "The agent reads HEARTBEAT.md (or a custom prompt file) as its instructions, "
                "runs all checks defined there, and only notifies the user when something needs attention. "
                "If nothing needs attention, the run is silent (NOTHING_TO_FLAG suppression)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Unique name for this heartbeat (e.g. 'daily-check', 'monitor').",
                    },
                    "schedule": {
                        "type": "string",
                        "description": "Interval string: 'every 30m', 'every 1h', 'every 6h', etc.",
                    },
                    "prompt_file": {
                        "type": "string",
                        "description": "Path to .md file with heartbeat instructions. Default: ~/.hermes/HEARTBEAT.md",
                    },
                    "deliver": {
                        "type": "string",
                        "description": "Delivery target. Default: 'origin' (same chat as where it was created).",
                    },
                    "model": {
                        "type": "string",
                        "description": "Model for heartbeat runs. Default: deepseek/deepseek-v4-flash",
                    },
                    "quiet_hours_start": {
                        "type": "string",
                        "description": "Start of quiet hours (24h format). Default: '23:00'",
                    },
                    "quiet_hours_end": {
                        "type": "string",
                        "description": "End of quiet hours (24h format). Default: '07:00'",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Timezone for quiet hours. Default: 'America/New_York'",
                    },
                    "enabled": {
                        "type": "boolean",
                        "description": "Whether the heartbeat is active. Default: true",
                    },
                },
                "required": ["name", "schedule"],
            },
        },
        toolset="heartbeat",
        description="Create a recurring heartbeat that wakes a lightweight agent on a schedule.",
        emoji="💓",
        check_fn=lambda: True,
    )

    # -- heartbeat_list --
    ctx.register_tool(
        name="heartbeat_list",
        handler=lambda args, **kw: heartbeat_list_tool(),
        schema={
            "name": "heartbeat_list",
            "description": "List all configured heartbeats with their status, schedule, and run history.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
        toolset="heartbeat",
        description="List all configured heartbeats with status.",
        emoji="📋",
        check_fn=lambda: True,
    )

    # -- heartbeat_run --
    ctx.register_tool(
        name="heartbeat_run",
        handler=lambda args, **kw: heartbeat_run_tool(
            name=args.get("name", ""),
        ),
        schema={
            "name": "heartbeat_run",
            "description": (
                "Manually trigger a heartbeat run immediately, ignoring the schedule and quiet hours. "
                "Useful for testing or forcing an immediate check."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the heartbeat to trigger.",
                    },
                },
                "required": ["name"],
            },
        },
        toolset="heartbeat",
        description="Manually trigger a heartbeat run immediately.",
        emoji="▶️",
        check_fn=lambda: True,
    )

    # Hooks — only pre_gateway_dispatch needed (no more post_llm_call)
    ctx.register_hook("pre_gateway_dispatch", capture_gateway)

    # Start scheduler only in ONE process — use a cross-process flock so that
    # the first process to load the plugin (typically the gateway) wins the lock
    # and runs the scheduler. All other processes (WebUI sessions, CLI, etc.)
    # skip scheduler startup entirely, preventing duplicate heartbeat spawns.
    if _try_acquire_scheduler_lock():
        _ensure_scheduler()
        logger.info("heartbeat plugin registered (v2.1 — subprocess execution, flock-guarded scheduler)")
    else:
        logger.info("heartbeat plugin registered (v2.1 — scheduler already running in another process, skipping)")
