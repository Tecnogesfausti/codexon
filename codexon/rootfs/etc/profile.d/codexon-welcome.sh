#!/usr/bin/env sh
if [ -n "${CODEX_CODEXON_WELCOME_SHOWN:-}" ]; then
  return 0 2>/dev/null || exit 0
fi
export CODEX_CODEXON_WELCOME_SHOWN=1
cat <<'EOF'

Codexon

Comandos principales:
  codex --model "$CODEX_MODEL" "$WORKSPACE"
      Abre Codex CLI para modificar, enseñar, corregir y ampliar Codexon o Home Assistant.

  codexon-console
      Controla el servicio Codexon: estado, tareas, agentes, logs y contexto.

  codexon-chat
      Abre el chat completo de codexon.py con memoria, MCP y herramientas.

  codexon-teach "tema a corregir"
      Registra una leccion para que Codex corrija Codexon.

Ayuda rapida:
  tail -f /data/codexon/codexon-service.log
  tail -f /data/codexon/codexon-runtime.log
  cat /share/codexon/runtime.txt

EOF
