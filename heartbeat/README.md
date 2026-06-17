# Heartbeat Plugin

A proactive scheduled agent runner for Hermes. The plugin wakes a lightweight LLM on a configurable schedule, gives it your HEARTBEAT.md as instructions (the prompt IS the program), and only notifies you when something needs attention.

## How It Works

```
1. Timer fires (every 30m, configurable)
2. Plugin reads HEARTBEAT.md from disk
3. Plugin reads last state from SQLite (what was already flagged)
4. Plugin injects a synthetic message into your session
5. A cheap LLM runs with FULL agent tools — it calls whatever HEARTBEAT.md told it to
6. Response comes back:
   - NOTHING_TO_FLAG → silent, no delivery
   - Findings → delivered to you automatically
```

## Setup

### 1. Enable the Plugin

Add `heartbeat` to your agent's enabled plugins:

```yaml
plugins:
  enabled:
    - heartbeat
```

### 2. Create HEARTBEAT.md

Write your check instructions at `~/.hermes/HEARTBEAT.md`:

```markdown
# Heartbeat

You are a proactive personal assistant. Run these checks:

1. Use lcm_grep to review the last 24 hours of conversation for any plans,
   commitments, or schedule mentions.

2. Use hindsight_recall to check my known schedule and upcoming obligations.

3. Cross-reference everything. Flag:
   - Schedule conflicts
   - Time-sensitive items I haven't addressed
   - Things I mentioned wanting to do that I haven't scheduled

4. If nothing new needs attention, respond: NOTHING_TO_FLAG

Rules:
- Do not repeat items from the previous state summary
- Be brief — one line per finding
- Only flag NEW information or changes since last check
- When in doubt, NOTHING_TO_FLAG is preferred over noise
```

### 3. Create a Heartbeat

Use the `heartbeat_create` tool in a chat with your agent:

```
heartbeat_create(
    name="daily-check",
    schedule="every 30m",
    model="deepseek/deepseek-v4-flash"
)
```

## Tools

### heartbeat_create

Create a recurring heartbeat.

**Required:** `name`, `schedule`

**Optional:**
- `prompt_file` — path to .md file (default: ~/.hermes/HEARTBEAT.md)
- `model` — model for heartbeat runs (default: deepseek/deepseek-v4-flash)
- `quiet_hours_start` — start of quiet period (default: 23:00)
- `quiet_hours_end` — end of quiet period (default: 07:00)
- `timezone` — timezone for quiet hours (default: America/New_York)
- `enabled` — active or not (default: true)
- `deliver` — delivery target (default: origin chat)

**Schedule formats:** `every 30m`, `every 1h`, `every 6h`

### heartbeat_list

List all configured heartbeats with status, last run, next run, and run count.

### heartbeat_run

Manually trigger a heartbeat immediately (ignores schedule and quiet hours). Pass the `name` parameter.

## Architecture

- **Background scheduler** — daemon thread polls every 10 seconds
- **SQLite state** — persistent storage at ~/.hermes/data/heartbeat.db (WAL mode)
- **NOTHING_TO_FLAG suppression** — post_llm_call hook catches silent responses
- **Quiet hours** — heartbeats skip during quiet periods, reschedule automatically
- **Per-heartbeat locks** — prevents overlapping runs of the same heartbeat
- **Model override** — runs on a cheap/fast model, not your main agent model

## State Files

- **Config DB:** `~/.hermes/data/heartbeat.db`
- **Prompt file:** `~/.hermes/HEARTBEAT.md` (or custom path per heartbeat)

## Phase 1 (Current)

- Background timer via pre_gateway_dispatch
- HEARTBEAT.md as the prompt (the prompt IS the program)
- Model override (cheap LLM)
- SQLite state persistence
- NOTHING_TO_FLAG suppression
- heartbeat_create, heartbeat_list, heartbeat_run
- Quiet hours support

## Coming in Phase 2

- State deduplication (prevents re-notification)
- heartbeat_pause, heartbeat_resume, heartbeat_remove
- heartbeat_status with detailed run history
- "While you were sleeping" batch summary after quiet hours
