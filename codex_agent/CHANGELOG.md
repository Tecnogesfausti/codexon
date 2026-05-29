# Changelog

## 0.1.28

- La terminal web vuelve a adjuntar a una sesión persistente `tmux` llamada `codex-agent`.
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

- El panel web escribe logs y runtime en `/share/codex-agent/` para poder leerlos desde File Browser o desde otro PC.

## 0.1.16

- Añadido `web-terminal.sh` con trazas a `/data/logs/web-terminal.log`.
- Redirigida la salida de `ttyd` a ese mismo log para diagnosticar pantallas en blanco.

## 0.1.15

- Bump de versión para publicar la terminal web directa con tema visible.

## 0.1.14

- Bump de versión para publicar la terminal web visible y directa.

## 0.1.13

- La terminal web vuelve a compartir sesión vía `tmux`, ahora con el patrón `ttyd tmux -u new -A -s codex-agent bash -l`.
- El panel lateral debería reconectar a la misma shell en vez de abrir una sesión vacía.

## 0.1.12

- Simplificada la terminal web para arrancar un login shell directo en `WORKSPACE`.
- Eliminado `tmux` de la ruta de arranque del panel web para evitar fallos `execvp`.

## 0.1.11

- `ttyd` ahora invoca `bash` directamente con el wrapper de terminal.
- `codex-agent-shell` usa `tmux new-session -A` para evitar fallos al adjuntar.

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
- Añadido wrapper `codex-agent-shell` para abrir la terminal en el workspace configurado.
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
