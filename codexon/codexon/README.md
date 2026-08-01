# Codexon

Agente de terminal 24/7 con:

- conexión MCP HTTP hacia Home Assistant
- conexión MCP por `stdio` como alternativa
- OpenRouter compatible con DeepSeek
- memoria local en SQLite
- observación periódica de sensores
- motor de eventos persistente para cambios de estado y eventos de Home Assistant
- comandos interactivos para consultar memoria, sensores y herramientas

## Instalación

```bash
cd /home/lego/sensores/Codexon
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Configuración

```bash
export OPENROUTER_API_KEY="sk-or-..."
export CODEXON_MODEL_ROUTES="model_routes.yaml"
export CODEXON_AGENTS_DIR="agents"
export CODEXON_LOG_FILE="codexon_runtime.log"
export HA_MCP_URL="http://supervisor/core/api/mcp"
export HA_TOKEN="token-de-larga-duracion-de-home-assistant"
export CODEXON_POLL_SECONDS="300"
export CODEXON_SENSOR_PROMPT="Consulta sensores de presencia, puertas, ventanas, movimiento, alarma y cámaras si existen."
export CODEXON_FS_ROOTS="/home/lego/sensores/Codexon"
```

También puedes poner esas variables en `.env`. Ese archivo está ignorado por git.

## Arranque con Home Assistant MCP HTTP

Si tienes `HA_MCP_URL` y `HA_TOKEN` en `.env`:

```bash
python3 codexon.py
```

O explícitamente:

```bash
python3 codexon.py --mcp-url http://supervisor/core/api/mcp
```

## Arranque con MCP por stdio

Si algún día usas un servidor MCP externo por comando, pásalo después de `--`.

Ejemplo de forma:

```bash
python3 codexon.py -- npx -y <servidor-mcp-home-assistant>
```

## Uso

Dentro del terminal:

```text
/help
/memoria
/sensores
/agentes
/agente iniciar monitor_temperatura
/agente run monitor_temperatura
/tareas
/cancelar 3
/editar 3 | 2026-07-10T23:00:00+02:00 | Encender luz | Enciende la luz del salon
/herramientas
/router
/coste
/logs
/salud
/estado
/aprender Mi horario normal entre semana es salir de casa a las 8:15.
/salir
```

## Ficheros Del Sistema

Codexon puede listar, leer, crear y borrar ficheros dentro de las rutas permitidas por `CODEXON_FS_ROOTS`.

Además el agente tiene herramientas internas para:

- leer sensores desde ficheros JSON, YAML, CSV o `clave=valor` con `sensor_read_file`
- contar líneas, frases y coincidencias exactas en ficheros con `fs_count_text`
- guardar/exportar datos en JSON, JSONL o texto con `data_write_file`
- consultar URLs públicas `http/https` y extraer texto básico con `web_fetch_url`

Las herramientas de ficheros siguen limitadas por `CODEXON_FS_ROOTS`. La herramienta web bloquea destinos locales/privados básicos y no reenvía cabeceras sensibles como `Authorization` o `Cookie`.

Comandos directos:

```text
/ls .
/leer README.md
/escribir notas/prueba.txt | contenido de prueba
/borrar notas/prueba.txt
```

Para permitir más rutas, sepáralas por coma:

```bash
export CODEXON_FS_ROOTS="/home/lego/sensores/Codexon,/home/lego/documentos"
```

Dar acceso a todo el sistema con `/` es posible si el usuario del proceso tiene permisos, pero es peligroso:

```bash
export CODEXON_FS_ROOTS="/"
```

## Herramientas Home Assistant Extra

Además de las herramientas MCP de Home Assistant, Codexon añade herramientas REST internas cuando hay `HA_MCP_URL` y `HA_TOKEN`:

- `ha_get_states`: listar estados actuales con filtros por dominio/texto
- `ha_get_state`: leer una entidad concreta
- `ha_search_entities`: buscar entidades por texto
- `ha_get_services`: listar servicios disponibles
- `ha_call_service`: ejecutar servicios genéricos con confirmación
- `ha_render_template`: renderizar plantillas Jinja de Home Assistant
- `ha_get_events`: listar tipos de eventos
- `ha_get_error_log`: leer el final del log de errores
- `ha_get_history`: consultar histórico
- `ha_get_logbook`: consultar logbook
- `ha_teach_entity_mapping`: enseñar alias, reemplazos y eliminaciones permanentes

`ha_call_service` requiere `confirm=true` cuando la configuración exige confirmación para acciones.

## Enseñanza Permanente De Entidades

Las correcciones explícitas se guardan en SQLite y prevalecen sobre el catálogo,
los alias de Home Assistant y `site.yaml`. Sirven para añadir nombres naturales,
sustituir dispositivos conservando su función o retirar una enseñanza anterior.
Cada cambio conserva fecha, origen, notas y estado activo para poder auditarlo.

Ejemplos:

```text
corrige el grifo del estanque por switch.riego2_rele3
cambia sensor.temperatura_viejo por sensor.temperatura_nuevo
pon el alias temperatura del porche como sensor.temperatura_porche
quita el alias temperatura del porche
lista las correcciones de entidades
```

Si la frase no permite verificar un destino único, Codexon no guarda el cambio y
pide una entidad concreta. Estas enseñanzas cambian cómo se resuelven acciones
futuras, pero no accionan dispositivos en el momento de aprender.

## Motor De Eventos

En modo servicio, Codexon mantiene una única conexión WebSocket con Home Assistant y
restaura automáticamente las escuchas guardadas en SQLite. Una escucha puede filtrar:

- tipo de evento y campos de `event.data`
- entidad y transición `from_state`/`to_state`
- atributos y operadores `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in`
- tiempo de enfriamiento (`cooldown_seconds`) y ejecución única (`once_only`)

Cuando hay coincidencia, el motor no ejecuta el modelo dentro del lector WebSocket:
encola una tarea normal e independiente, registra el disparo y continúa escuchando.
Las herramientas `create_event_listener`, `list_event_listeners` y
`cancel_event_listener` permiten administrarlas desde la conversación. La API web
equivalente está disponible en `/api/event-listeners`.

## Coste Y Tokens

Codexon guarda el uso de tokens y el coste estimado de cada llamada al modelo en SQLite.

```text
/coste
/estado
```

El coste es estimado usando la tabla pública de precios de OpenRouter al arrancar. Si OpenRouter no devuelve tokens para una llamada, esa llamada queda registrada sin coste estimado.

## Seleccion Inteligente De Modelos

Todas las llamadas al LLM pasan por `ModelRouter`. Las reglas viven en `model_routes.yaml`, no en el codigo.

```yaml
default: openai/gpt-4.1-nano

routes:
  homeassistant:
    model: openai/gpt-4.1-nano
    requires_tools: true

  summary:
    model: google/gemini-2.5-flash-lite
```

Puedes ver la configuracion cargada con:

```text
/router
```

Y el log de uso con modelo elegido, motivo, tokens, duracion y coste con:

```text
/coste
```

## Agentes Especializados - Fase 1

Los agentes viven en `agents/` y heredan de `agents.base.Agent`.

Comandos:

```text
/agentes
/agente descubrir
/agente iniciar <nombre>
/agente detener <nombre>
/agente reiniciar <nombre>
/agente run <nombre>
```

Fase 1 incluye:

- clase base `Agent`
- metadatos obligatorios del agente
- `AgentManager`
- descubrimiento dinamico de modulos en `agents/`
- inicio/detencion/reinicio logico
- ejecucion manual `run`
- estadisticas y aislamiento de errores

La ejecucion programada automatica de agentes queda para Fase 2.

## Historial De Consola

Codexon usa `readline` cuando está disponible para editar la línea con el cursor y recuperar comandos con flecha arriba/abajo. El historial se guarda por defecto en `.codexon_history`.

Puedes configurarlo con:

```bash
export CODEXON_HISTORY_FILE=".codexon_history"
export CODEXON_HISTORY_LIMIT="1000"
```

## Supervisión Y Logs

Codexon escribe eventos en `CODEXON_LOG_FILE` en formato JSONL. Esto permite supervisar lo que hace mientras está en marcha:

```bash
tail -f codexon_runtime.log
```

Comandos internos:

```text
/logs
/logs 100
/salud
```

`/salud` resume MCP, ModelRouter, agentes, tareas fallidas y coste acumulado.

## Funcionamiento 24/7

Mientras el proceso esté vivo, Codexon:

1. mantiene una conversación interactiva por terminal
2. usa herramientas MCP para consultar Home Assistant cuando lo necesite
3. observa sensores cada `CODEXON_POLL_SECONDS`
4. guarda observaciones y memorias en `codexon_memory.sqlite3`
5. ejecuta tareas programadas persistentes

Para desactivar el observador periódico:

```bash
python3 codexon.py --no-sensor-loop -- npx -y <servidor-mcp-home-assistant>
```

## Seguridad

Por defecto, el agente tiene instrucción de no cambiar estados sensibles como alarmas, sirenas, cerraduras, cámaras o automatizaciones sin confirmación explícita. Para permitir acciones directas:

```bash
python3 codexon.py --allow-actions-without-confirmation -- npx -y <servidor-mcp-home-assistant>
```

## Tareas Futuras

Puedes pedir tareas en lenguaje natural:

```text
Esta noche enciende la luz de 23:00 a 3:00 del dia siguiente.
```

El agente debe crear dos tareas:

- una para encender la luz a las 23:00
- otra para apagarla a las 03:00 del dia siguiente

Comandos útiles:

```text
/tareas
/tareas --todo
/cancelar <id>
/reintentar <id>
/editar <id> | <ISO> | <titulo> | <instruccion>
```

Las tareas simples de recordatorio se imprimen directamente sin llamar al modelo. Las tareas que requieren entender o actuar sobre Home Assistant tienen timeout configurable:

```bash
export CODEXON_TASK_TIMEOUT_SECONDS=45
```

## Home Assistant Add-on

El add-on publicable está en la carpeta superior `codexon/` del monorepo. El núcleo de este directorio se empaqueta dentro de esa única imagen; no existe un segundo add-on anidado.

En Home Assistant:

1. Ve a Ajustes -> Complementos -> Tienda de complementos.
2. Abre el menú de los tres puntos y entra en Repositorios.
3. Añade: `https://github.com/Tecnogesfausti/codexon`
4. Instala el add-on `Codexon`.
5. En la configuración del add-on pon `openrouter_api_key`.
6. Arranca el add-on y abre el portal único desde Ingress.

Codexon y su web quedan activos por defecto. El portal ofrece las estadísticas y la terminal Codex sin publicar un segundo puerto HTTP.


### Monitor de temperatura

`monitor_temperatura` lee sensores de temperatura de Home Assistant por REST, usando `HA_TOKEN` y la URL configurada. Por defecto detecta sensores con `device_class=temperature` o unidad de temperatura. Puedes restringirlo con patrones:

```bash
MONITOR_TEMPERATURA_ENTITIES="sensor.salon_*,sensor.huerto_temperatura"
MONITOR_TEMPERATURA_MIN_C="5"
MONITOR_TEMPERATURA_MAX_C="35"
MONITOR_TEMPERATURA_STALE_MINUTES="60"
MONITOR_TEMPERATURA_RAPID_DELTA_C="4"
MONITOR_TEMPERATURA_RAPID_WINDOW_MINUTES="30"
```

El agente solo avisa y guarda observaciones; no acciona climatizacion sin confirmacion.


### Monitor de dispositivos caidos

`monitor_dispositivos_caidos` revisa entidades de Home Assistant y detecta estados `unavailable/unknown`, entidades demasiado antiguas y baterias bajas. Solo propone mantenimiento; no reinicia ni cambia dispositivos automaticamente.

```bash
MONITOR_DISPOSITIVOS_ENTITIES="sensor.*,binary_sensor.*,switch.*,light.*"
MONITOR_DISPOSITIVOS_IGNORE="sensor.ruidoso,binary_sensor.lento"
MONITOR_DISPOSITIVOS_STALE_MINUTES="120"
MONITOR_DISPOSITIVOS_VERY_STALE_MINUTES="720"
MONITOR_DISPOSITIVOS_LOW_BATTERY_PERCENT="20"
```


### Monitor de trafico

`monitor_trafico` usa `live_context` y el proveedor `traffic_dgt` para vigilar incidencias en la ubicación configurada. Las carreteras también se configuran por instalación. No inventa datos: si no hay fuente DGT configurada, avisa de que falta `DGT_TRAFFIC_URL`.

```bash
DGT_TRAFFIC_ENABLED="true"
DGT_TRAFFIC_URL="https://.../fuente-dgt-o-datex.json"
MONITOR_TRAFICO_ROADS="A-1,M-30"
MONITOR_TRAFICO_MIN_SEVERITY="info"
```

El agente solo recomienda revisar ruta alternativa; no inicia navegacion ni modifica calendario.


### Monitor de calidad del aire

`monitor_calidad_aire` consulta Open-Meteo Air Quality para los puntos configurados, manteniendo cada lectura separada.

```bash
OPEN_METEO_AIR_QUALITY_ENABLED="true"
AIR_QUALITY_LOCATIONS="Casa|<latitud>|<longitud>;Trabajo|<latitud>|<longitud>"
MONITOR_CALIDAD_AIRE_MAX_EXERCISE_AQI="60"
MONITOR_CALIDAD_AIRE_MAX_VENTILATION_AQI="80"
```

El agente diferencia prediccion/modelo de medicion observada y solo recomienda prudencia; no ejecuta acciones fisicas.








### Terminal tmux persistente

El add-on mantiene una terminal persistente basada en `ttyd + tmux`, integrada en el mismo portal Ingress `8099`. La terminal sólo escucha internamente y siempre reengancha a la sesión `codexon`.

```yaml
web_terminal_enabled: true
```

La terminal arranca en `workspace` y conserva estado aunque cierres el navegador. Logs:

```text
/share/codexon/web-terminal.log
```

Desde ella puedes abrir `CODEX_CONTEXT.md`, revisar logs, lanzar Codex o trabajar en el workspace Git sin parar el servicio Codexon. El acceso se mantiene dentro de la autenticación Ingress de Home Assistant.

### Configuracion Codex interactivo

El add-on configura Codex CLI con estas opciones:

```yaml
codex_model: "gpt-5.3-codex"
codex_home: "/data/codex"
workspace: "/ha_config"
mcp_server_url: "http://supervisor/core/api/mcp"
```

El add-on exporta `CODEX_MODEL`, `CODEX_HOME` y `WORKSPACE`, y crea esos directorios al arrancar. `workspace` es el sitio recomendado para clonar el repo Codexon y dejar que Codex modifique codigo, haga commits y prepare backups.

### Backup cifrado de memoria

Codexon puede crear un backup de reconstruccion que incluye memoria SQLite, tareas, observaciones, configuracion de agentes, agentes vivos, contexto Codex, notas y logs. Desde la UI usa **Backups** o llama `POST /api/backup`.

Config recomendada:

```yaml
backup_key: "una-clave-larga-privada"
```

El backup se guarda en:

```text
/data/codexon/backups/codexon-backup-YYYYMMDDTHHMMSSZ.tar.gz.enc
```

Se cifra con OpenSSL AES-256-CBC + PBKDF2. Para descifrar:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -in codexon-backup-XXXX.tar.gz.enc -out codexon-backup.tar.gz -pass pass:TU_CLAVE
tar -tzf codexon-backup.tar.gz
```

Ese `.enc` se puede copiar al workspace Git y subirlo a GitHub como backup privado, siempre que la clave no se suba al repositorio.

### Contexto para Codex interactivo

El panel web genera un contexto de mantenimiento para una sesion Codex/tmux externa o futura:

- `CODEX_CONTEXT.md`: estado vivo de Codexon, rutas, agentes, tareas, observaciones y cola de logs.
- `CODEX_NOTES.md`: notas humanas o de Codex para enseñar criterios, errores conocidos y decisiones de diseño.

En el add-on se guardan en:

```text
/data/codexon/CODEX_CONTEXT.md
/data/codexon/CODEX_NOTES.md
```

Desde la UI usa **Codex mantenimiento** para refrescar el contexto y guardar notas. Desde una terminal Codex, abre primero `CODEX_CONTEXT.md` y usa esas rutas para corregir o extender Codexon.

### Configuracion compartida HA/MCP

Codexon usa estas opciones para Home Assistant y MCP:

```yaml
home_assistant_token: ""
ha_long_lived_token: ""
mcp_server_url: ""
mcp_server_api_key: ""
```

Precedencia del token HA: `home_assistant_token`, despues `ha_long_lived_token`, y por ultimo `SUPERVISOR_TOKEN`. Si `mcp_server_url` queda vacio se usa `http://supervisor/core/api/mcp`. `mcp_server_api_key` cae a `SUPERVISOR_TOKEN` o al token HA.

### Servicio persistente en el add-on

El add-on arranca Codexon como servicio 24/7 en segundo plano mientras mantiene el panel web abierto. La opción `codexon_enabled` controla el núcleo y `codexon_web_enabled` controla la web. En modo servicio se usa `codexon.py --service`, que mantiene MCP, scheduler de tareas y observadores sin abrir el prompt interactivo.

```yaml
codexon_enabled: true
codexon_web_enabled: true
```

Con esta combinación, la web queda disponible por Ingress `8099` y las tareas creadas desde la UI son ejecutadas por el proceso Codexon residente. Los logs del servicio se escriben en `/data/codexon/codexon-service.log`.

La terminal persistente tipo `tmux` queda integrada como herramienta de mantenimiento y desarrollo. No es el motor del scheduler: Codexon ya puede esperar, ejecutar tareas y guardar resultados de forma persistente desde su proceso de servicio.

### Panel web operativo

La UI web permite gestionar Codexon desde el navegador:

- crear tareas con fecha/hora, prioridad e intervalo informativo;
- ejecutar, cancelar o eliminar tareas;
- listar agentes, ejecutar un agente manualmente y ajustar prioridad/intervalo efectivo;
- guardar configuracion de agentes en `CODEXON_AGENT_CONFIG`;
- ver observaciones, resultados, herramientas y logs.

Variables utiles:

```bash
CODEXON_DB=/data/codexon/codexon_memory.sqlite3
CODEXON_AGENT_CONFIG=/data/codexon/agent_config.json
CODEXON_AGENTS_DIR=agents
```

## Live Context Manager

Codexon incluye una primera vertical de `LiveContextManager` en `services/live_context/`.

Estado actual:

- ubicación obligatoria por instalación, sin coordenadas privadas en el repositorio
- proveedor implementado: Open-Meteo Forecast API
- agente implementado: `monitor_clima_exterior`
- caché en memoria con TTL
- timeout y reintentos por cliente HTTP
- fallback a datos stale si falla una fuente tras haber tenido datos válidos
- tests sin Internet con `httpx.MockTransport`

Variables principales:

```bash
LIVE_CONTEXT_LOCATION="Municipio, Provincia, País"
LIVE_CONTEXT_LAT="<latitud>"
LIVE_CONTEXT_LON="<longitud>"
LIVE_CONTEXT_RADIUS_KM="20"
LIVE_CONTEXT_TIMEZONE="Europe/Madrid"
OPEN_METEO_ENABLED="true"
LIVE_CONTEXT_HTTP_TIMEOUT_SECONDS="15"
LIVE_CONTEXT_MAX_RETRIES="2"
```

Prueba manual dentro de Codexon:

```text
/agentes
/agente run monitor_clima_exterior
```
# WhatsApp interno (HAWhatsUp/Baileys)

En modo servicio, Codexon puede consumir el núcleo Baileys proporcionado por el
add-on Codexon. El proceso se supervisa desde el servicio y los mensajes
viajan por stdin/stdout en JSON Lines: no se necesita MQTT, un servidor HTTP,
puertos ni autenticación entre procesos.

```bash
export CODEXON_WHATSAPP_ENABLED=true
export CODEXON_WHATSAPP_DATA_DIR=/data/codexon/whatsapp
export CODEXON_WHATSAPP_BRIDGE=/opt/codexon/whatsapp-core/bridge.mjs
python3 codexon.py --service
```

La primera vinculación muestra el QR en el panel web de Codexon. Autenticación,
contactos y estado se conservan dentro del directorio de datos. Por defecto se
aceptan chats privados y se ignoran grupos, histórico y mensajes enviados por
la propia cuenta para evitar bucles. La lista opcional
`CODEXON_WHATSAPP_ALLOWED_SENDERS` permite restringir el canal. Las palabras
de activación se configuran en `CODEXON_WHATSAPP_WAKE_WORDS`, separadas por
`|` (por ejemplo, `casa|huerto`). No distinguen mayúsculas ni acentos y se
eliminan antes de entregar la orden a Codexon.
