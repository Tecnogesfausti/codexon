#!/usr/bin/env sh
if [ -n "${CODEX_SEGURAI_WELCOME_SHOWN:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
export CODEX_SEGURAI_WELCOME_SHOWN=1
cat <<'EOF'

Codex Agent + SegurAI

Comandos principales:
  codex --model "$CODEX_MODEL" "$WORKSPACE"
      Abre Codex para modificar, enseñar, corregir y ampliar SegurAI o Home Assistant.

  segurai-console
      Abre el prompt interactivo del servicio SegurAI: estado, tareas, agentes, logs y contexto.

Ayuda rapida:
  tail -f /data/segurai/segurai-service.log
  tail -f /data/segurai/segurai-runtime.log
  cat /share/codex-agent/runtime.txt

EOF
