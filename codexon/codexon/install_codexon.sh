#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo "[Codexon] preparando entorno en: $(pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: falta python3" >&2
  exit 1
fi

if [ ! -f ".env" ]; then
  echo "ERROR: falta .env con las variables y claves de Codexon" >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m py_compile codexon.py agents/base.py agents/manager.py agents/monitor_temperatura.py tools/common.py tools/filesystem.py tools/homeassistant.py tools/registry.py tools/web.py

chmod +x codexon.py

echo ""
echo "[Codexon] listo."
echo ""
echo "Arranque normal:"
echo "  source .venv/bin/activate"
echo "  python3 codexon.py"
echo ""
echo "Arranque de prueba sin observador automatico:"
echo "  source .venv/bin/activate"
echo "  python3 codexon.py --no-sensor-loop"
echo ""
echo "Comandos utiles dentro de Codexon:"
echo "  /salud"
echo "  /router"
echo "  /coste"
echo "  /agentes"
echo "  /logs 50"
