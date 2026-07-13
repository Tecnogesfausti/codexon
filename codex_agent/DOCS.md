# Codex Agent

Codex Agent ejecuta Codex CLI dentro de Home Assistant como add-on con permisos amplios. Está orientado a administrar configuración, add-ons locales, carpetas compartidas y servicios accesibles mediante Supervisor.

## Panel lateral

El add-on usa Ingress y aparece en la barra lateral de Home Assistant como **Codex Agent**, igual que otros terminales web. El panel abre una terminal `ttyd` dentro del contenedor.

Desde ese panel puedes ejecutar:

```sh
codex-login-chatgpt
codex /ha_config
ha-states
ha-services
host-shell
```

## Qué monta

- `/ha_config`: configuración de Home Assistant.
- `/addon_config`: configuración propia del add-on.
- `/all_addon_configs`: configuraciones de todos los add-ons.
- `/addons`: add-ons locales.
- `/share`, `/media`, `/backup`, `/ssl`: carpetas estándar de Home Assistant.

Todos estos montajes están en modo lectura/escritura.

## Tokens

Home Assistant inyecta `SUPERVISOR_TOKEN` automáticamente cuando `hassio_api` y `homeassistant_api` están activados. Este add-on también permite configurar `home_assistant_token` para llamadas directas o persistentes contra la API de Home Assistant. `ha_long_lived_token` se mantiene como alias compatible.

Usa un token dedicado:

1. En Home Assistant, abre tu perfil de usuario.
2. Crea un Long-Lived Access Token.
3. Pégalo en la opción `home_assistant_token`.
4. Revócalo si dejas de usar el agente.

Con ese token, Codex puede leer todos los estados y llamar servicios mediante:

```sh
ha-states
ha-services
ha-api GET /states
ha-api GET /services
ha-call-service homeassistant restart '{}'
```

## Codex

El contenedor instala Codex CLI con:

```sh
npm install -g @openai/codex
```

La forma recomendada es iniciar sesión con ChatGPT/Codex desde una terminal del contenedor:

```sh
codex-login-chatgpt
```

El comando usa autenticación por código de dispositivo. Abres el enlace en tu navegador, introduces el código y Codex guarda la sesión en `/data/codex/auth.json`. No hace falta `openai_api_key` para este modo.

Después puedes lanzar una sesión manual:

```sh
codex --model "$CODEX_MODEL" "$WORKSPACE"
```

Por defecto `WORKSPACE=/ha_config`.

`openai_api_key` solo es necesaria si prefieres usar facturación de OpenAI API por uso.

## MCP

Si `install_mcp_servers` está activo, el arranque registra servidores MCP con `codex mcp add`:

- `ha-config`: filesystem sobre `/ha_config`, `/addon_config`, `/all_addon_configs`, `/addons` y `/share`.
- `memory`: servidor de memoria local.
- `remote-home-assistant`: servidor remoto si configuras `mcp_server_url`.

`mcp_server_url` es la única opción MCP obligatoria para Home Assistant. Si rellenas `mcp_server_api_key`, se usa esa. Si no hay `SUPERVISOR_TOKEN` en la sesión, el add-on cae automáticamente a `http://homeassistant:8123/api/mcp` y usa `home_assistant_token`.

## Helpers

El add-on incluye estos comandos:

```sh
ha-api GET /config
ha-states
ha-services
ha-call-service light turn_on '{"entity_id":"light.example"}'
supervisor-api GET /addons
esphome version
esphome-version-check
host-shell
```

Ejemplos:

```sh
ha-api POST /services/homeassistant/restart '{}'
ha-states | grep '^sensor\.'
ha-services
supervisor-api GET /addons/core_configurator/info
esphome-version-check
esphome version
host-shell
```

`host-shell` usa `nsenter` contra el proceso 1 del host. Requiere que la instalación respete `host_pid: true` y los privilegios declarados por el add-on. Es la vía para inspección avanzada del sistema cuando los montajes estándar de Home Assistant no bastan.

El arranque del panel web escribe en `/share/codex-agent/web-terminal.log` y deja un resumen en `/share/codex-agent/runtime.txt`. Si la pantalla sale en blanco, ese archivo es el primer sitio donde mirar desde File Browser o desde otro PC.


## SegurAI opcional

El add-on puede arrancar SegurAI como servicio 24/7 sin tocar la terminal Codex. La terminal lateral sigue siendo `ttyd + tmux` en Ingress `8099`; SegurAI se copia a `/data/segurai/app` en el primer arranque y se ejecuta desde esa ruta para que Codex pueda modificarlo de forma persistente.

Opciones principales:

```yaml
openrouter_api_key: "sk-or-..."
segurai_enabled: true
segurai_web_enabled: false
ingress_target: "codex"
segurai_poll_seconds: 300
segurai_fs_roots: "/ha_config,/addon_config,/share"
```

Logs:

```sh
tail -f /data/segurai/segurai-service.log
tail -f /data/segurai/segurai-runtime.log
```

El botón Ingress del add-on siempre entra por `8099`, pero `ingress_target` decide qué se ve tras reiniciar: `codex` deja la terminal en el botón y SegurAI web en `8098`; `segurai` pone SegurAI web en el botón y mueve la terminal Codex a `8098`. En ese modo la web de SegurAI arranca aunque `segurai_web_enabled` esté en `false`.
Desde la terminal Codex puedes controlar el servicio real sin arrancar otro `segurai.py`:

```sh
segurai-console
```

La consola usa historial y flecha arriba. Si escribes texto libre, lo envía al servicio SegurAI como una tarea inmediata y espera el resultado:

```text
busca retenciones en la A7 cerca de Torrent
```

Comandos útiles dentro del prompt:

```text
/estado
/tareas
/crear Probar fichero | Escribe 100 veces no memarees en mareo.txt
/agentes
/logs 120
/salir
```

`segurai-console` habla con la API web local de SegurAI en `8098` o `8099`, según `ingress_target`.

Para conversar con el SegurAI completo, usando `segurai.py` real, memoria, MCP y herramientas:

```sh
segurai-chat
```

`segurai-chat` usa bloqueo para evitar dos sesiones simultáneas y siempre arranca con `--no-sensor-loop`, de modo que no duplica los observadores 24/7 del servicio.


Al abrir la terminal aparece un banner con los dos caminos principales: `codex --model "$CODEX_MODEL" "$WORKSPACE"` para trabajar sobre código y `segurai-console` para operar el servicio.


## SSH opcional

El acceso principal es el panel lateral por Ingress. SSH queda como acceso alternativo: activa `ssh_enabled` y añade claves públicas en `ssh_public_keys`. El puerto interno es `2222/tcp`; asigna un puerto de host desde la pantalla del add-on si quieres entrar por SSH.

Una vez dentro:

```sh
codex-login-chatgpt
codex /ha_config
```

## Riesgos

Este add-on se declara con:

- `full_access: true`
- `protected: false`
- `apparmor: false`
- `host_pid`, `host_network`, `host_dbus`, `host_ipc`, `host_uts`
- `hassio_role: admin`
- montajes RW de configuración y add-ons

Eso permite cambiar o romper el sistema con facilidad. Úsalo solo en redes y máquinas de confianza.
