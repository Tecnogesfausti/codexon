# Codexon Portable Bundle

Este paquete contiene lo necesario para continuar Codexon en otro equipo:

- codigo principal: `codexon.py`
- agentes: `agents/`
- reglas de modelos: `model_routes.yaml`
- dependencias: `requirements.txt`
- instalador local: `install_codexon.sh`
- memoria/estado SQLite: `codexon_memory.sqlite3`
- variables y claves: `.env`
- documentacion: `README.md`

## Instalacion En Otro Equipo

```bash
tar -xzf codexon-portable-YYYYMMDD-HHMMSS.tar.gz
cd Codexon
./install_codexon.sh
```

## Arranque

```bash
source .venv/bin/activate
python3 codexon.py --no-sensor-loop
```

Luego prueba:

```text
/salud
/router
/agentes
/coste
```

## Seguridad

El archivo `.env` incluye claves/tokens. Trata este paquete como secreto.
