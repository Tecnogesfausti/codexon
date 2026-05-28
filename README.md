# Home Assistant Codex Agent

Repositorio de add-on para ejecutar Codex CLI dentro de Home Assistant con permisos amplios sobre el sistema supervisado.

> Este add-on está pensado para uso propio y entornos controlados. Con `full_access`, montajes de escritura y tokens persistentes, cualquier sesión de Codex puede modificar configuración, add-ons, ficheros compartidos y otros servicios accesibles desde el Supervisor.

## Add-on incluido

- `codex_agent`: entorno Debian con Codex CLI, Node.js, Python, herramientas de desarrollo, cliente MCP básico y utilidades para operar contra Home Assistant/Supervisor.

## Instalación local

1. Copia este repositorio en `/addons/ha_codex_agent` dentro de Home Assistant OS/Supervised, o añádelo como repositorio de add-ons si lo publicas en GitHub.
2. En Home Assistant, ve a **Settings > Add-ons > Add-on Store > Check for updates**.
3. Instala **Codex Agent**.
4. Deja `openai_api_key` vacío si vas a iniciar sesión con tu cuenta de ChatGPT/Codex desde la terminal.
5. Define `home_assistant_token` con un Long-Lived Access Token dedicado para que Codex pueda leer sensores, entidades y servicios.
6. Si usas un Model Context Protocol Server externo, rellena `mcp_server_url` y `mcp_server_api_key`.
7. Activa `ssh_enabled` y añade tu clave pública SSH para poder entrar al contenedor y ejecutar `codex-login-chatgpt`.

## Acceso a sensores y servicios

El add-on incluye helpers dentro del contenedor:

```sh
ha-states
ha-services
ha-api GET /states
ha-api GET /services
ha-call-service light turn_on '{"entity_id":"light.example"}'
```

Codex recibe un `AGENTS.md` generado en `/data/codex` con estas instrucciones para que consulte estados y servicios vivos antes de cambiar YAML o reiniciar add-ons.

## Seguridad operativa

- Mantén `require_confirmation: true` mientras lo pruebes.
- No expongas el puerto SSH/Web a Internet.
- Usa un token de Home Assistant dedicado, con rotación manual y revocación fácil.
- Revisa los cambios antes de reiniciar Home Assistant, ESPHome, AppDaemon u otros add-ons.
