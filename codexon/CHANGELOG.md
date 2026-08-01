# Changelog

## 0.3.1

- Codexon usa una red de contenedor aislada para poder convivir con instalaciones anteriores sin competir por el puerto Ingress interno `8099`.

## 0.3.0

- Trasladado el add-on y el núcleo Codexon al monorepo `Tecnogesfausti/codexon`.
- Unificada toda la identidad del producto como Codexon: add-on, slug, panel, comandos, módulos, rutas, variables, logs, base de datos, WhatsApp, documentación y pruebas.
- Los comandos de servicio son ahora `codexon-console`, `codexon-chat` y `codexon-teach`.
- El add-on arranca automáticamente y activa por defecto el núcleo Codexon y su panel web.
- Eliminado el manifiesto de add-on anidado y obsoleto para que Supervisor descubra únicamente Codexon 0.3.0.
- Codexon se copia desde el propio contexto de construcción; la imagen ya no clona ni depende de los repositorios antiguos.
- GitHub CLI (`gh`) queda instalado permanentemente para autenticar, revisar repositorios y publicar cambios desde Codex.

## 0.2.9

- Corregida la migración del núcleo Codexon persistente: cada revisión sincroniza todo el código empaquetado, incluidas herramientas, servicios e intents.
- Los archivos instalados que cambien se conservan antes en `/data/codexon/backups/runtime-0.2.9-previous`; memorias, sesiones de WhatsApp y demás datos quedan fuera de la sincronización.

## 0.2.8

- La barra `Ctrl`, `Esc`, `Tab` y flechas se muestra siempre dentro de Terminal, sin depender de cómo el WebView Android anuncie el puntero o el ancho.
- Añadido versionado de los scripts incrustados y cabeceras sin caché para evitar que Home Assistant Companion reutilice interfaces anteriores.
- Eliminadas las opciones redundantes para mensajes propios y grupos de WhatsApp; ambos tipos quedan aceptados internamente por defecto.

## 0.2.7

- Añadida a Estadísticas una pestaña de memorias recientes con fecha, tipo, tema, contenido, confianza y origen.
- El resumen de memorias se carga de forma independiente para que un fallo transitorio de otra API no deje el panel incompleto.
- Los errores de carga se muestran dentro de la sección afectada y permiten reintentar sin recargar toda la Ingress.

## 0.2.6

- Añadida una barra de teclas táctiles en la terminal para Android y otros dispositivos móviles.
- Incluidos `Esc`, `Tab`, flechas, un modificador `Ctrl` para la siguiente tecla y los atajos habituales `Ctrl+C`, `Ctrl+X`, `Ctrl+Z`, `Ctrl+L`, `Ctrl+D` y `Ctrl+R`.
- La barra sólo ocupa espacio en pantallas táctiles o estrechas y mantiene el foco en ttyd.

## 0.2.5

- Unificada la interfaz del add-on en una sola Ingress `8099`, con pestañas para estadísticas Codexon y terminal Codex.
- Codexon web y ttyd quedan accesibles sólo mediante el proxy interno, sin publicar un segundo puerto HTTP.
- La terminal conserva su sesión `ttyd + tmux`, el teclado interactivo y el portapapeles dentro del nuevo panel.
- `ingress_target` queda obsoleto y se mantiene sólo por compatibilidad con configuraciones existentes.

## 0.2.4

- Añadida la opción portable `codexon_whatsapp_wake_words` para configurar varias palabras de activación separadas por `|`.
- Sustituido el antiguo prefijo único de WhatsApp por palabras de activación configurables.

## 0.2.3

- Persistidos contactos y mensajes recientes del núcleo WhatsApp.
- Conservados nombres de contactos frente a actualizaciones con identificadores numéricos.
- Preparado el historial entrante y saliente para las nuevas herramientas de Codexon.

## 0.2.2

- Fijado Codexon a MCP 1.x para conservar la API `streamablehttp_client`.
- Añadida migración con copia de seguridad para actualizar los archivos de ejecución persistentes de Codexon.
- Corregido el arranque de la web y del adaptador WhatsApp tras actualizar desde una instalación existente.

## 0.2.1

- Integrado el núcleo Baileys de HAWhatsUp como servicio interno del add-on Codexon.
- La mensajería usa JSON Lines por stdin/stdout, sin MQTT, HTTP ni puertos adicionales.
- Añadida persistencia de sesión, reconexión automática y QR en el panel existente de Codexon.

## 0.2.0

- Codexon incorpora perfiles locales `site.yaml` para adaptar entidades, alias e instrucciones a cada instalación sin publicar datos privados.
- Añadido motor versionado de automatizaciones, planificación determinista, scheduler robusto y herramientas ampliadas de Home Assistant.
- Añadidos historial agregado, TTS y notificaciones, catálogo de memoria, contexto ambiental configurable y soporte opcional para Traccar.
- Eliminados valores locales de ubicación, coordenadas, dispositivos y contraseñas de los ejemplos distribuibles.

## 0.1.34

- Añadido `codexon-teach` para registrar errores/lecciones y preparar contexto de corrección para Codex.
- `codexon-console` guarda la última interacción, etiqueta errores (`ERROR_TIMEOUT`, `ERROR_TASK_FAILED`, `USER_TEACHING`) y añade `/ensenar` y `/corregir`.

## 0.1.33

- Añadido `codexon-chat`, wrapper seguro para abrir el `codexon.py` completo desde la terminal Codex.
- `codexon-chat` reutiliza memoria/MCP/OpenRouter, fuerza `--no-sensor-loop` y usa bloqueo para evitar sesiones duplicadas.

## 0.1.32

- `codexon-console` acepta texto libre en modo chat: crea una tarea inmediata, espera el resultado y lo muestra en el prompt.

## 0.1.31

- Añadido `codexon-console`, un prompt interactivo con historial para controlar el servicio Codexon desde la terminal Codex.
- Permite consultar estado, logs, tareas, agentes y crear tareas hablando con la API web local de Codexon.
- Añadido banner de bienvenida en la terminal con los comandos principales `codex` y `codexon-console`.

## 0.1.30

- Añadida opción `ingress_target` para alternar el botón Ingress entre terminal Codex y panel web Codexon tras reiniciar.
- Cuando `ingress_target: "codexon"`, Codexon web escucha en `8099` y la terminal Codex pasa a `8098`.

## 0.1.29

- Añadido Codexon como servicio opcional dentro del add-on Codex funcional.
- La terminal `ttyd + tmux` e Ingress `8099` se mantienen sin cambios.
- Codexon se inicializa en `/data/codexon/app` para que Codex pueda modificarlo de forma persistente.
- Añadido panel web opcional de Codexon en el puerto directo `8098`.

## 0.1.28

- La terminal web vuelve a adjuntar a una sesión persistente `tmux` llamada `codexon`.
- Al reconectar desde el panel lateral, se recupera la misma shell en vez de arrancar otra sesión.

## 0.1.27

- PlatformIO usa `/data/codex/platformio` como caché persistente para compilaciones ESPHome.
- Exportadas `PLATFORMIO_CORE_DIR` y `PLATFORMIO_PACKAGES_DIR` para evitar fallback a `/tmp`.

## 0.1.26

- Añadido ESPHome CLI al build del add-on mediante un entorno virtual en `/opt/esphome`.
- Añadido `esphome-version-check` para avisar al arrancar si PyPI publica una versión más nueva.

## 0.1.25

- Si `SUPERVISOR_TOKEN` no está disponible, `ha-api` usa la API directa de Home Assistant en `http://homeassistant:8123/api`.
- `remote-home-assistant` cae automáticamente a `http://homeassistant:8123/api/mcp` cuando no hay `SUPERVISOR_TOKEN`.

## 0.1.24

- `ha-api` y `remote-home-assistant` priorizan `SUPERVISOR_TOKEN` para el proxy interno `http://supervisor/core/api`.
- `home_assistant_token` queda como fallback, no como credencial principal para esas rutas.

## 0.1.23

- `ha-api` y el MCP remoto vuelven a priorizar el token largo de Home Assistant.
- `SUPERVISOR_TOKEN` queda solo como fallback final.

## 0.1.22

- `remote-home-assistant` y `ha-api` priorizan `SUPERVISOR_TOKEN` para acceso interno a Home Assistant.
- El token largo del usuario queda como override explícito, no como valor por defecto para rutas internas.

## 0.1.21

- `remote-home-assistant` ahora reutiliza `home_assistant_token` como fallback de autenticación.
- Evita tener que duplicar el mismo token en dos campos distintos.

## 0.1.20

- Cambiada la grabación del terminal web de `tee` a `script -f -a` para conservar un TTY real.
- `codex .` vuelve a ver `stdout` como terminal interactiva.

## 0.1.19

- La terminal web duplica la salida con `tee`, así se ve en pantalla y también queda en el log.

## 0.1.18

- Eliminadas opciones no soportadas por `ttyd 1.7.7` (`--reconnect`).
- Restaurado `--writable` para que la terminal web acepte entrada.

## 0.1.17

- El panel web escribe logs y runtime en `/share/codexon/` para poder leerlos desde File Browser o desde otro PC.

## 0.1.16

- Añadido `web-terminal.sh` con trazas a `/data/logs/web-terminal.log`.
- Redirigida la salida de `ttyd` a ese mismo log para diagnosticar pantallas en blanco.

## 0.1.15

- Bump de versión para publicar la terminal web directa con tema visible.

## 0.1.14

- Bump de versión para publicar la terminal web visible y directa.

## 0.1.13

- La terminal web vuelve a compartir sesión vía `tmux`, ahora con el patrón `ttyd tmux -u new -A -s codexon bash -l`.
- El panel lateral debería reconectar a la misma shell en vez de abrir una sesión vacía.

## 0.1.12

- Simplificada la terminal web para arrancar un login shell directo en `WORKSPACE`.
- Eliminado `tmux` de la ruta de arranque del panel web para evitar fallos `execvp`.

## 0.1.11

- `ttyd` ahora invoca `bash` directamente con el wrapper de terminal.
- `codexon-shell` usa `tmux new-session -A` para evitar fallos al adjuntar.

## 0.1.10

- El panel web usa `tmux` como backend para resistir desconexiones.
- `ttyd` arranca con `--reconnect 30` y `--ping-interval 2` para reducir cortes de websocket.

## 0.1.9

- Migrado el registro MCP al comando `codex mcp add`, que es lo que Codex CLI usa de verdad.
- Eliminadas opciones MCP muertas que no se estaban aplicando.

## 0.1.8

- Añadido paquete `bubblewrap` para que Codex use el sandbox del sistema sin avisos.

## 0.1.7

- Eliminado `build.yaml` obsoleto.
- Movidos los parámetros de build al Dockerfile.

## 0.1.6

- Corregida instalación de `ttyd`: Debian bookworm no lo publica como paquete apt, ahora se descarga el binario oficial `1.7.7` para `amd64`/`aarch64`.

## 0.1.5

- Corregido schema de `mcp_config` y `mcp_server_headers`: Home Assistant no acepta `dict?`, ahora se configuran como JSON en texto.
- Eliminadas arquitecturas antiguas para evitar avisos de Supervisor.

## 0.1.4

- Añadidas arquitecturas `armhf`, `armv7` e `i386` para que el Store no oculte el add-on en instalaciones de 32 bits.
- Actualizada etiqueta Docker `io.hass.type` al valor actual `app`.

## 0.1.3

- Añadido panel lateral por Home Assistant Ingress.
- Añadida terminal web `ttyd` en el puerto interno `8099`.
- Añadido wrapper `codexon-shell` para abrir la terminal en el workspace configurado.
- SSH queda como acceso alternativo opcional.

## 0.1.2

- Documentado flujo principal con inicio de sesión ChatGPT/Codex en vez de API key.
- Añadido helper `codex-login-chatgpt` para autenticación por código de dispositivo en entornos headless.
- Configurado almacenamiento de credenciales de Codex en fichero persistente bajo `/data/codex`.
- Evitado exportar `OPENAI_API_KEY` cuando la opción está vacía.

## 0.1.1

- Añadidas opciones explícitas `home_assistant_token`, `mcp_server_url`, `mcp_server_api_key` y `mcp_server_headers`.
- Añadidos helpers `ha-states`, `ha-services` y `ha-call-service`.
- Generado contexto `AGENTS.md` para orientar Codex hacia estados y servicios vivos de Home Assistant.
- Soporte para registrar un MCP remoto en `/data/codex/mcp-servers.json`.

## 0.1.0

- Scaffold inicial del add-on.
- Instalación de Codex CLI mediante `@openai/codex`.
- Montajes RW para configuración de Home Assistant, add-ons, backups, media, share y SSL.
- Acceso a Home Assistant API y Supervisor API.
- Servidores MCP filesystem/memory opcionales.
- SSH opcional mediante claves públicas.
