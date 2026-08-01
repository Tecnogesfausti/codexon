#!/usr/bin/env sh
set -eu

set -a
[ -r /config/codexon.env ] && . /config/codexon.env
set +a

mkdir -p "${CODEX_HOME:-/data/codex}" "${WORKSPACE:-/config/codexon-dev}" /config/data
cd "${WORKSPACE:-/config/codexon-dev}"

cat <<'EOF'
Codexon terminal persistente

Esta terminal usa tmux. Puedes cerrar el navegador y volver sin perder la sesion.

Comandos utiles:
  cd "$WORKSPACE"
  cat /config/data/CODEX_CONTEXT.md
  tail -f /config/data/codexon_service.log
  tail -f /config/data/codexon_runtime.log
  python3 /app/codexon.py --help

EOF

exec tmux -u new-session -A -s codexon-terminal /bin/bash -l
