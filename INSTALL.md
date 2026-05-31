# kanban-context plugin — Quick Install Guide

Kanban context injection + cross-bot message bus plugin for Hermes Agent.

**Version:** v1.0.0 | **Source:** kaishi00/hermes-community-plugins
**Compatibility:** Hermes v0.13+ | Python 3.11+ | Stdlib only (zero dependencies)

---

## 📦 Installation

### Manual (direct copy)
```bash
# 1. Clone the repository
git clone https://github.com/kaishi00/hermes-community-plugins.git

# 2. Copy plugins to Hermes
cp -r hermes-community-plugins/kanban-context ~/.hermes/plugins/kanban-context
cp -r hermes-community-plugins/multi-agent-context ~/.hermes/plugins/multi-agent-context

# 3. Add to your profile's config.yaml
# plugins:
#   enabled:
#     - multi-agent-context
#     - kanban-context

# 4. Restart the gateway
hermes gateway restart
```

> ⚠️ **Note:** `multi-agent-context` is required for the cross-bot message bus. Without it, Kanban activity injection still works, but inter-bot messaging won't.

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KANBAN_CONTEXT_EVENT_LIMIT` | `10` | Max events injected per context block |
| `KANBAN_CONTEXT_LOOKBACK_H` | `12` | Lookback window (hours) |
| `KANBAN_CONTEXT_CLEANUP_INTERVAL` | `86400` | Maintenance interval (seconds, default 24h) |
| `KANBAN_CONTEXT_OUTBOX_RETENTION` | `14` | Days to keep completed messages |
| `KANBAN_CONTEXT_LOG_RETENTION` | `7` | Days to keep log files |
| `CROSSBOT_BOT_NAME` | *(profile name)* | Bot name for bus addressing |
| `MULTI_AGENT_TG_DB_PATH` | `$HERMES_HOME/data/multi_agent_tg_shared.db` | Shared SQLite DB path |

### Install-time Validation

On load, the plugin automatically validates:
- ✅ Python >= 3.11
- ✅ Hermes Agent compatible
- ✅ `multi-agent-context` plugin installed
- ✅ Shared database accessible
- ✅ Bot name resolved
- ✅ Environment variables valid

All checks appear in gateway logs at startup — no surprises at runtime.

---

## 🎯 Features

### 1. Kanban Activity Injection
Every agent sees what's happening on the boards: tasks created, moved, completed, blocked — without needing to query explicitly.

### 2. Cross-Bot Message Bus
Telegram bots can communicate with each other via a shared `outbox` table in SQLite. Useful for:
- Delegating tasks between profiles (e.g., IT requests analysis from CRM)
- Coordinating agents without depending on platform APIs

```python
# Programmatic usage
from plugins.kanban_context import crossbot_send, crossbot_respond

# Send a message to another bot
msg_id = crossbot_send(
    to_bot="profile_name",
    subject="Message subject",
    body="Message body"
)

# Respond to a message
crossbot_respond(msg_id, "Response here")
```

### 3. Auto-Cleaning
Lightweight automatic maintenance that runs on every LLM call:
- 🗑️ Deletes completed messages > 14 days
- ⏰ Marks pending messages > 7 days as abandoned
- 📁 Removes logs > 7 days

### 4. Dashboard `/kanban-status`
Send `/kanban-status` to any agent running the plugin and receive:
- Plugin version and config
- Bot name and paths
- Discovered boards and their sizes
- Bus statistics (pending/done)
- Overall health (✅ or ⚠️ with details)

---

## 📋 Public API

```python
from plugins.kanban_context import (
    # Cross-bot messaging
    crossbot_send,        # (to_bot, subject, body) -> outbox_id
    crossbot_respond,     # (outbox_id, response_text) -> bool
    crossbot_get_history, # (for_bot, limit) -> list[dict]

    # Maintenance
    run_maintenance,      # (force=False) -> None

    # Dashboard
    kanban_status,        # () -> str (formatted report)
)
```

---

## 🩺 Manual Health Check

```bash
# Quick validation test
python3 -c "
import importlib.util, sys, os
spec = importlib.util.spec_from_file_location(
    'kc', os.path.expanduser('~/.hermes/plugins/kanban-context/__init__.py')
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
vr = mod.run_validation()
print(f'Errors: {len(vr.errors)}, Warnings: {len(vr.warnings)}')
for e in vr.errors: print(f'  ❌ {e}')
for w in vr.warnings: print(f'  ⚠️  {w}')
"
```

---

*Original Portuguese version by [franklinbravos](https://github.com/franklinbravos). English translation by maintainers.*
