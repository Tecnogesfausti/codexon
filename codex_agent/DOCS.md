# Codex Agent

Codex Agent ejecuta Codex CLI dentro de Home Assistant como add-on con permisos amplios. Está orientado a administrar configuración, add-ons locales, carpetas compartidas y servicios accesibles mediante Supervisor.

## Panel lateral

El add-on usa Ingress y aparece en la barra lateral de Home Assistant como **Codex Agent**, igual que otros terminales web. El panel abre una terminal `ttyd` dentro del contenedor en el workspace configurado.

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

Si `install_mcp_servers` está activo, el arranque genera `/data/codex/mcp-servers.json` con:

- `ha-config`: servidor MCP filesystem sobre carpetas de Home Assistant.
- `memory`: servidor MCP de memoria.
- `remote-home-assistant`: servidor MCP remoto si configuras `mcp_server_url`.

Opciones MCP:

- `mcp_server_url`: URL del Model Context Protocol Server externo.
- `mcp_server_api_key`: clave para ese servidor. Se envía como `Authorization: Bearer <clave>`.
- `mcp_server_headers`: cabeceras adicionales.
- `mcp_config`: configuración libre guardada en `/data/mcp/config.json`.

## Helpers

El add-on incluye dos comandos:

```sh
ha-api GET /config
ha-states
ha-services
ha-call-service light turn_on '{"entity_id":"light.example"}'
supervisor-api GET /addons
host-shell
```

Ejemplos:

```sh
ha-api POST /services/homeassistant/restart '{}'
ha-states | grep '^sensor\.'
ha-services
supervisor-api GET /addons/core_configurator/info
host-shell
```

`host-shell` usa `nsenter` contra el proceso 1 del host. Requiere que la instalación respete `host_pid: true` y los privilegios declarados por el add-on. Es la vía para inspección avanzada del sistema cuando los montajes estándar de Home Assistant no bastan.

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
