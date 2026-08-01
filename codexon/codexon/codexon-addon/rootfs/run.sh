#!/usr/bin/env sh
set -eu

CONFIG_DIR=/config
APP_DIR=/app
OPTIONS=/data/options.json
ENV_FILE="${CONFIG_DIR}/codexon.env"
SERVICE_LOG="${CONFIG_DIR}/data/codexon_service.log"
TERMINAL_LOG="${CONFIG_DIR}/data/codexon_terminal.log"

mkdir -p "${CONFIG_DIR}" "${CONFIG_DIR}/agents" "${CONFIG_DIR}/data"

python3 - <<'PY'
import json
import os
import shlex
from pathlib import Path

options_path = Path('/data/options.json')
options = json.loads(options_path.read_text(encoding='utf-8')) if options_path.exists() else {}
config_dir = Path('/config')
env_path = config_dir / 'codexon.env'

def value(name, default=''):
    item = options.get(name, default)
    return item if item is not None else default

def env_line(name, val):
    return f"{name}={shlex.quote(str(val))}"

home_assistant_token = value('home_assistant_token')
ha_long_lived_token = value('ha_long_lived_token')
ha_token = home_assistant_token or ha_long_lived_token or os.environ.get('SUPERVISOR_TOKEN', '')
mcp_server_url = value('mcp_server_url') or 'http://supervisor/core/api/mcp'
mcp_server_api_key = value('mcp_server_api_key') or os.environ.get('SUPERVISOR_TOKEN', '') or ha_token
mcp_token_source = (
    'mcp_server_api_key'
    if value('mcp_server_api_key')
    else 'SUPERVISOR_TOKEN'
    if os.environ.get('SUPERVISOR_TOKEN')
    else 'HA_TOKEN'
    if ha_token
    else 'none'
)
if not os.environ.get('SUPERVISOR_TOKEN') and mcp_server_url in ('', 'http://supervisor/core/api/mcp'):
    mcp_server_url = 'http://homeassistant:8123/api/mcp'

codex_model = value('codex_model', 'gpt-5.3-codex')
codex_home = value('codex_home', '/data/codex')
workspace = value('workspace', '/config/codexon-dev')

lines = [
    env_line("OPENROUTER_API_KEY", value('openrouter_api_key', '')),
    env_line("CODEXON_MODEL_ROUTES", f"/app/{value('model_routes', 'model_routes.yaml')}"),
    env_line("CODEXON_AGENTS_DIR", "/config/agents"),
    env_line("CODEXON_DB", "/config/data/codexon_memory.sqlite3"),
    env_line("CODEXON_SITE_PROFILE", "/config/data/site.yaml"),
    env_line("CODEXON_AGENT_CONFIG", "/config/data/agent_config.json"),
    env_line("CODEXON_CODEX_CONTEXT", "/config/data/CODEX_CONTEXT.md"),
    env_line("CODEXON_CODEX_NOTES", "/config/data/CODEX_NOTES.md"),
    env_line("CODEXON_BACKUP_DIR", "/config/data/backups"),
    env_line("CODEXON_BACKUP_KEY", value('backup_key')),
    env_line("CODEX_MODEL", codex_model),
    env_line("CODEX_HOME", codex_home),
    env_line("WORKSPACE", workspace),
    env_line("CODEXON_WORKSPACE", workspace),
    env_line("CODEXON_LOG_FILE", "/config/data/codexon_runtime.log"),
    env_line("CODEXON_POLL_SECONDS", value('poll_seconds', 300)),
    env_line("CODEXON_SENSOR_PROMPT", value('sensor_prompt', '')),
    env_line("CODEXON_FS_ROOTS", value('fs_roots', '/config,/share')),
    env_line("HA_MCP_URL", mcp_server_url),
    env_line("MCP_SERVER_URL", mcp_server_url),
    env_line("MCP_SERVER_API_KEY", mcp_server_api_key),
    env_line("MCP_AUTH_TOKEN", mcp_server_api_key),
    env_line("TRACCAR_BASE_URL", value('traccar_base_url', '')),
    env_line("TRACCAR_API_TOKEN", value('traccar_api_token', '')),
    env_line("CODEXON_MCP_TOKEN_SOURCE", mcp_token_source),
    env_line("HOME_ASSISTANT_URL", "http://supervisor/core"),
    env_line("HA_TOKEN", ha_token),
    env_line("HOME_ASSISTANT_TOKEN", ha_token),
    env_line("HA_LONG_LIVED_TOKEN", value('ha_long_lived_token')),
]
env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
env_path.chmod(0o600)
PY

for file in "${APP_DIR}"/agents/*.py; do
  cp -n "$file" "${CONFIG_DIR}/agents/$(basename "$file")" || true
done
mkdir -p "${CONFIG_DIR}/agents/pending"

set -a
. "${ENV_FILE}"
set +a

echo "[Codexon add-on] MCP URL: ${HA_MCP_URL} (token: ${CODEXON_MCP_TOKEN_SOURCE})"

mkdir -p "$CODEX_HOME" "$WORKSPACE"

read_bool() {
  key="$1"
  fallback="$2"
  python3 - "$key" "$fallback" <<'PY'
import json
import sys
from pathlib import Path
key, fallback = sys.argv[1], sys.argv[2]
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print('true' if options.get(key, fallback == 'true') else 'false')
PY
}

WEB_ONLY=$(read_bool web_only true)
AGENT_SERVICE_ENABLED=$(read_bool agent_service_enabled true)
TERMINAL_ENABLED=$(read_bool terminal_enabled true)
TERMINAL_PORT=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(int(options.get('terminal_port', 8098)))
PY
)
TERMINAL_USERNAME=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(options.get('terminal_username') or 'codexon')
PY
)
TERMINAL_PASSWORD=$(python3 - <<'PY'
import json
from pathlib import Path
options = json.loads(Path('/data/options.json').read_text(encoding='utf-8')) if Path('/data/options.json').exists() else {}
print(options.get('terminal_password') or '')
PY
)
NO_SENSOR_LOOP=$(read_bool no_sensor_loop false)
ALLOW_ACTIONS=$(read_bool allow_actions_without_confirmation false)

ARGS=""
if [ "$NO_SENSOR_LOOP" = "true" ]; then
  ARGS="$ARGS --no-sensor-loop"
fi
if [ "$ALLOW_ACTIONS" = "true" ]; then
  ARGS="$ARGS --allow-actions-without-confirmation"
fi

run_agent_service_forever() {
  while true; do
    echo "[Codexon add-on] starting Codexon task worker" | tee -a "$SERVICE_LOG"
    python3 codexon.py --service $ARGS >>"$SERVICE_LOG" 2>&1
    status=$?
    echo "[Codexon add-on] Codexon task worker exited with status ${status}; restarting in 5s" | tee -a "$SERVICE_LOG"
    sleep 5
  done
}

if [ "$AGENT_SERVICE_ENABLED" = "true" ]; then
  run_agent_service_forever &
fi

if [ "$TERMINAL_ENABLED" = "true" ] && [ -n "$TERMINAL_PASSWORD" ]; then
  echo "[Codexon add-on] starting tmux terminal on ${TERMINAL_PORT}" | tee -a "$TERMINAL_LOG"
  ttyd \
    --port "$TERMINAL_PORT" \
    --interface 0.0.0.0 \
    --credential "${TERMINAL_USERNAME}:${TERMINAL_PASSWORD}" \
    --writable \
    --terminal-type xterm-256color \
    --client-option titleFixed="Codexon Terminal" \
    --client-option cursorBlink=true \
    --client-option cursorStyle=bar \
    --client-option disableLeaveAlert=true \
    /usr/local/bin/codexon-terminal.sh >>"$TERMINAL_LOG" 2>&1 &
elif [ "$TERMINAL_ENABLED" = "true" ]; then
  echo "[Codexon add-on] terminal disabled: configure a non-empty terminal_password" | tee -a "$TERMINAL_LOG"
fi

if [ "$WEB_ONLY" = "true" ]; then
  exec python3 -m uvicorn codexon_web:app --host 0.0.0.0 --port 8099
fi

if [ "$AGENT_SERVICE_ENABLED" = "true" ]; then
  exec tail -F "$SERVICE_LOG"
fi

exec python3 codexon.py $ARGS
