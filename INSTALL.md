# kanban-context plugin — Guia de Instalação Rápida

Plugin de injeção de contexto Kanban + barramento de mensagens cross-bot para Hermes Agent.

**Versão:** v2.1.0 | **Fonte:** franklinbravos/hermes-community-plugins
**Compatibilidade:** Hermes v0.13+ | Python 3.11+ | Stdlib only (zero dependências)

---

## 📦 Instalação

### Via `hermes plugins install` (recomendado)
```bash
hermes plugins install https://github.com/franklinbravos/hermes-community-plugins
```

### Manual (cópia direta)
```bash
# 1. Clone o repositório
git clone https://github.com/franklinbravos/hermes-community-plugins.git

# 2. Copie os plugins para o Hermes
cp -r hermes-community-plugins/kanban-context ~/.hermes/plugins/kanban-context
cp -r hermes-community-plugins/multi-agent-context ~/.hermes/plugins/multi-agent-context

# 3. Adicione ao config.yaml do profile
# plugins:
#   enabled:
#     - multi-agent-context
#     - kanban-context

# 4. Restarte o gateway
hermes gateway restart
```

> ⚠️ **Nota:** O `multi-agent-context` é obrigatório para o barramento cross-bot funcionar. Sem ele, a injeção de atividade Kanban ainda funciona, mas as mensagens entre bots não.

---

## ⚙️ Configuração

### Variáveis de Ambiente

| Variável | Default | Descrição |
|----------|---------|-----------|
| `KANBAN_CONTEXT_EVENT_LIMIT` | `10` | Máx. de eventos injetados por contexto |
| `KANBAN_CONTEXT_LOOKBACK_H` | `12` | Janela de busca (horas) |
| `KANBAN_CONTEXT_CLEANUP_INTERVAL` | `86400` | Intervalo de manutenção (segundos, default 24h) |
| `KANBAN_CONTEXT_OUTBOX_RETENTION` | `14` | Dias para manter mensagens concluídas |
| `KANBAN_CONTEXT_LOG_RETENTION` | `7` | Dias para manter arquivos de log |
| `CROSSBOT_BOT_NAME` | *(nome do profile)* | Nome do bot para endereçamento no barramento |
| `MULTI_AGENT_TG_DB_PATH` | `$HERMES_HOME/data/multi_agent_tg_shared.db` | Caminho do banco SQLite compartilhado |

### Validação na Instalação

Ao carregar, o plugin valida automaticamente:
- ✅ Python >= 3.11
- ✅ Hermes Agent compatível
- ✅ multi-agent-context instalado
- ✅ Banco de dados compartilhado acessível
- ✅ Nome do bot resolvido
- ✅ Variáveis de ambiente válidas

Tudo isso aparece nos logs do gateway na inicialização — sem surpresas em runtime.

---

## 🎯 Funcionalidades

### 1. Injeção de Atividade Kanban
Todo agente vê o que está acontecendo nos boards: tarefas criadas, movidas, completadas, bloqueadas — sem precisar consultar explicitamente.

### 2. Barramento Cross-Bot
Bots do Telegram conseguem se comunicar entre si via uma tabela `outbox` compartilhada em SQLite. Útil para:
- Delegar tarefas entre perfis (ex: TI pede análise pro CRM)
- Coordenar agentes sem depender de APIs de plataforma

```python
# Exemplo de uso programático
from plugins.kanban_context import crossbot_send, crossbot_respond

# Enviar mensagem para outro bot
msg_id = crossbot_send(
    to_bot="profile_name",
    subject="Assunto da mensagem",
    body="Corpo da mensagem"
)

# Responder a uma mensagem
crossbot_respond(msg_id, "Resposta aqui")
```

### 3. Auto-Cleaning
Manutenção automática e leve que roda a cada chamada LLM:
- 🗑️ Deleta mensagens concluídas > 14 dias
- ⏰ Marca mensagens pendentes > 7 dias como abandonadas
- 📁 Remove logs > 7 dias

### 4. Dashboard `/kanban-status`
Envie `/kanban-status` para qualquer agente rodando o plugin e receba:
- Versão do plugin e config
- Nome do bot e caminhos
- Boards descobertos e seus tamanhos
- Estatísticas do barramento (pending/done)
- Saúde geral (✅ ou ⚠️ com detalhes)

---

## 🔗 Repositórios

| Repositório | Link |
|-------------|------|
| **Fork (ativo)** | https://github.com/franklinbravos/hermes-community-plugins |
| **Upstream** | https://github.com/kaishi00/hermes-community-plugins |
| **PR #1 (v2.1.0)** | https://github.com/kaishi00/hermes-community-plugins/pull/1 |
| **PR #2** | https://github.com/kaishi00/hermes-community-plugins/pull/2 |

---

## 📋 API Pública

```python
from plugins.kanban_context import (
    # Barramento cross-bot
    crossbot_send,        # (to_bot, subject, body) -> outbox_id
    crossbot_respond,     # (outbox_id, response_text) -> bool
    crossbot_get_history, # (for_bot, limit) -> list[dict]
    
    # Manutenção
    run_maintenance,      # (force=False) -> None
    
    # Dashboard
    kanban_status,        # () -> str (relatório formatado)
)
```

---

## 🩺 Health Check Manual

```bash
# Teste rápido de validação
python3 -c "
import importlib.util, sys, os
spec = importlib.util.spec_from_file_location(
    'kc', os.path.expanduser('~/.hermes/plugins/kanban-context/__init__.py')
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
vr = mod.run_validation()
print(f'Erros: {len(vr.errors)}, Avisos: {len(vr.warnings)}')
for e in vr.errors: print(f'  ❌ {e}')
for w in vr.warnings: print(f'  ⚠️  {w}')
"
```
