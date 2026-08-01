# Codexon

Codexon ejecuta Codex CLI dentro de Home Assistant como add-on con permisos amplios. Está orientado a administrar configuración, add-ons locales, carpetas compartidas y servicios accesibles mediante Supervisor.

## Panel lateral

El add-on usa Ingress y aparece en la barra lateral de Home Assistant como **Codexon**. El panel único ofrece dos pestañas: **Estadísticas** de Codexon y **Terminal** Codex.

La terminal continúa siendo `ttyd + tmux`: conserva la edición interactiva, las teclas especiales, el historial y la sesión al cambiar de pestaña o reconectar.

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

## GitHub

La imagen incluye GitHub CLI de forma permanente. Su configuración se guarda bajo `CODEX_HOME=/data/codex`, por lo que puede conservarse entre reinicios y actualizaciones del add-on:

```sh
gh auth login -h github.com
gh auth status
gh repo view Tecnogesfausti/codexon
```

No guardes tokens dentro del repositorio ni en ficheros del workspace. Usa siempre el almacén de credenciales de `gh`.

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

El arranque del panel web escribe en `/share/codexon/web-terminal.log` y deja un resumen en `/share/codexon/runtime.txt`. Si la pantalla sale en blanco, ese archivo es el primer sitio donde mirar desde File Browser o desde otro PC.


## Codexon opcional

El add-on puede arrancar Codexon como servicio 24/7 sin tocar la terminal Codex. Su fuente vive en `codexon/codexon`, se empaqueta junto con Codexon y se sincroniza a `/data/codexon/app` mediante migraciones con copia de seguridad.

Cada vivienda debe mantener su contexto fuera del repositorio. Para iniciar un perfil local:

```sh
cp /data/codexon/app/site.example.yaml /data/codexon/site.yaml
nano /data/codexon/site.yaml
```

Define ahí los roles, alias y entidades relevantes de esa instalación. Codexon también puede descubrir entidades vivas, pero el perfil evita ambigüedades y conserva las decisiones del propietario. No copies perfiles, credenciales ni bases de memoria entre viviendas.

Opciones principales:

```yaml
openrouter_api_key: "sk-or-..."
codexon_enabled: true
codexon_web_enabled: true
codexon_poll_seconds: 300
codexon_fs_roots: "/ha_config,/addon_config,/share"
```

Logs:

```sh
tail -f /data/codexon/codexon-service.log
tail -f /data/codexon/codexon-runtime.log
```

El add-on expone un único servidor web por Ingress `8099`. Dentro del contenedor, el portal envía `/stats/` a Codexon y `/terminal/` a ttyd; esos servicios internos sólo escuchan en `127.0.0.1` y no publican puertos adicionales. La opción antigua `ingress_target` se conserva únicamente para que las configuraciones instaladas sigan siendo válidas, pero ya no altera el panel ni los puertos.

Desde la terminal Codex puedes controlar el servicio real sin arrancar otro `codexon.py`:

```sh
codexon-console
```

La consola usa historial y flecha arriba. Si escribes texto libre, lo envía al servicio Codexon como una tarea inmediata y espera el resultado:

```text
busca retenciones cerca de casa
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

`codexon-console` habla con la API web local de Codexon en su puerto interno `8098`.

Para conversar con el Codexon completo, usando `codexon.py` real, memoria, MCP y herramientas:

```sh
codexon-chat
```

`codexon-chat` usa bloqueo para evitar dos sesiones simultáneas y siempre arranca con `--no-sensor-loop`, de modo que no duplica los observadores 24/7 del servicio.


Al abrir la terminal aparece un banner con los dos caminos principales: `codex --model "$CODEX_MODEL" "$WORKSPACE"` para trabajar sobre código y `codexon-console` para operar el servicio.



### Enseñar y corregir Codexon con Codex

Cuando Codexon responda mal o use una herramienta de forma incorrecta, desde `codexon-console` puedes guardar la última interacción como lección:

```text
/ensenar Debe resolver nombres parciales de entidades HA antes de consultar historico
```

Si quieres abrir Codex directamente para corregir código:

```text
/corregir Debe resolver sensor ITORRE692 a sensor.itorre692_temperature
```

También puedes hacerlo desde la shell:

```sh
codexon-teach "Corrige la consulta historica de temperatura alrededor de una hora"
```

Esto crea:

```text
/data/codexon/teach/latest.md
/data/codexon/teach/lessons.jsonl
/data/codexon/CODEX_NOTES.md
```

Codex debe revisar `latest.md`, tocar `/data/codexon/app`, ejecutar pruebas si existen y explicar el cambio. Los fallos quedan etiquetados con clases como `ERROR_TIMEOUT`, `ERROR_TASK_FAILED` o `USER_TEACHING`.

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
# WhatsApp directo con Codexon

Codexon incorpora y mantiene el núcleo Baileys de HAWhatsUp dentro de su
propia imagen. No usa MQTT, HTTP, puertos ni autenticación entre componentes:
Codex entrega el transporte a la tarea interna de Codexon y ambos se comunican
por una tubería privada JSON Lines.

Activa `codexon_whatsapp_enabled` y reinicia el add-on. En el primer arranque,
abre el panel existente de Codexon y vincula WhatsApp con el QR de la sección
**WhatsApp**. La sesión queda persistida en `/data/codexon/whatsapp/auth`.

`codexon_whatsapp_allowed_senders` es opcional. Vacío acepta mensajes privados
de cualquier remitente; si se rellena, solo acepta esos números internacionales
sin necesidad del signo `+`. `codexon_whatsapp_wake_words` permite exigir una
de varias palabras de activación separadas por `|`, por ejemplo
`casa|huerto|asistente|ia|robot`. No distingue mayúsculas ni acentos y elimina
la palabra antes de entregar la orden. Los grupos y los mensajes enviados por
la propia cuenta se aceptan siempre; no necesitan opciones separadas en la
configuración del add-on. Las palabras de activación siguen aplicándose antes
de entregar la orden a Codexon.
