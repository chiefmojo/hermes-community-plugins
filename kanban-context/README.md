# kanban-context plugin 🗂️

Injects recent Kanban board activity + cross-bot messaging into agent context
via the `pre_llm_call` hook.

**v2.1.0** — Now with proactive validation, auto-cleaning logs, and `/kanban-status` dashboard.

---

## Features

### 1. Kanban Activity Injection
Gives every agent awareness of what work items are flowing through the board —
task creation, moves, completions, blocks, worker heartbeats.

### 2. Cross-Bot Messaging (v2.0)
Because Telegram bots cannot see messages from other bots (hard API limitation),
this plugin implements a **cross-bot message bus** using a shared SQLite
`outbox` table.

**How it works:**
1. **Bot A** (sender) writes a message to the shared `outbox` table and creates a Kanban task assigned to **Bot B**
2. The **Kanban dispatcher** picks up the task and spawns a worker for Bot B
3. **Bot B** reads the task body (= the message), processes it, and can respond
4. **Bot B** marks the outbox as `done` and completes the Kanban task with a summary

**API for plugins/scripts:**
```python
from plugins.kanban_context import crossbot_send, crossbot_respond, crossbot_get_history

# Send a message to another bot profile
outbox_id = crossbot_send(
    to_bot="profile_name",
    subject="Check plugin version",
    body="Please verify the plugin.yaml version matches __init__.py",
    kanban_task_id="t_abc123"
)

# Respond to a message
crossbot_respond(outbox_id, "All versions match. Done!")
```

### 3. Proactive Install-time Validation (v2.1)
On plugin load, automatically checks:
- Python version (>= 3.11)
- Hermes Agent compatibility (v0.13+)
- `multi-agent-context` plugin presence (required dependency)
- Shared database accessibility and writability
- Kanban database / boards directory
- Bot name resolution for cross-bot messaging
- Environment variable sanity (numeric bounds)

Warnings and errors are logged at startup — no need to wait for runtime failures.

### 4. Auto-Cleaning Logs (v2.1)
Self-maintenance runs opportunistically on every `pre_llm_call` hook:
- **Outbox pruning**: deletes completed messages older than retention (default: 14 days)
- **Stale message cleanup**: marks pending messages older than 7 days as abandoned
- **Log rotation**: removes plugin log files older than retention (default: 7 days)

All intervals are configurable via environment variables.

### 5. Dashboard / `/kanban-status` (v2.1)
Send `/kanban-status` to any agent running the plugin to receive a full status
report including:
- Plugin version and configuration
- Connected bots and boards
- Outbox statistics
- Health check with validation results

Programmatic access: `kanban_status()` returns the same report as a string.

## Requirements
- Hermes Agent v0.13+ with plugin system
- Python 3.11+
- No extra dependencies (stdlib only)
- `multi-agent-context` plugin (recommended, for shared DB)

## Install
```bash
cp -r kanban-context ~/.hermes/plugins/kanban-context
```

Add to your profile's `config.yaml`:
```yaml
plugins:
  enabled:
    - kanban-context
```

Restart the gateway.

## Configuration (Environment Variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `KANBAN_CONTEXT_EVENT_LIMIT` | `10` | Max events to inject per pre-LLM context block |
| `KANBAN_CONTEXT_LOOKBACK_H` | `12` | Lookback window in hours |
| `CROSSBOT_BOT_NAME` | *(profile name)* | This bot's name for outbox addressing |
| `MULTI_AGENT_TG_DB_PATH` | `$HERMES_HOME/data/multi_agent_tg_shared.db` | Shared SQLite DB path |
| `KANBAN_CONTEXT_CLEANUP_INTERVAL` | `86400` | Maintenance interval in seconds (default: 24h) |
| `KANBAN_CONTEXT_OUTBOX_RETENTION` | `14` | Days to keep completed outbox messages |
| `KANBAN_CONTEXT_LOG_RETENTION` | `7` | Days to keep plugin log files |
| `KANBAN_CONTEXT_TOPIC_MAP` | *(empty)* | Topic-to-bot assignment: `chat_key=bot,...` |
| `KANBAN_CONTEXT_MENTION_MAP` | *(empty)* | Bot @mention mapping: `BotName=@username,...` |
| `CROSSBOT_BOT_NAME` | *(profile name)* | This bot's name for outbox + mention detection |

## Python API

```python
from plugins.kanban_context import (
    # Cross-bot messaging
    crossbot_send, crossbot_respond, crossbot_get_history,
    # Maintenance
    run_maintenance,
    # Dashboard
    kanban_status,
)
```

## Zero Hardcoding

This plugin has **zero hardcoded paths or bot names**. Everything is resolved
from Hermes Agent's standard paths (`HERMES_HOME`), environment variables,
or profile configuration. It works in any environment without modification.
