# Codexon

Monorepo autocontenido del add-on Codexon para Home Assistant. Incluye el núcleo del asistente, el portal web, la terminal Codex, WhatsApp y todas las herramientas de operación y desarrollo.

> Este add-on está pensado para uso propio y entornos controlados. Con `full_access`, montajes de escritura y tokens persistentes, cualquier sesión de Codex puede modificar configuración, add-ons, ficheros compartidos y otros servicios accesibles desde el Supervisor.

## Add-on incluido

- `codexon`: add-on con Codex CLI, GitHub CLI, portal único por Ingress, terminal web, WhatsApp y utilidades para operar contra Home Assistant/Supervisor.
- `codexon/codexon`: núcleo de Codexon empaquetado directamente en la imagen, sin descargar código durante la construcción.

## Instalación local

1. Añade `https://github.com/Tecnogesfausti/codexon` como repositorio de add-ons, o copia el repositorio en `/addons/codexon` dentro de Home Assistant OS/Supervised.
2. En Home Assistant, ve a **Settings > Add-ons > Add-on Store > Check for updates**.
3. Instala **Codexon**.
4. Deja `openai_api_key` vacío si vas a iniciar sesión con tu cuenta de ChatGPT/Codex desde la terminal.
5. Define `home_assistant_token` con un Long-Lived Access Token dedicado para que Codex pueda leer sensores, entidades y servicios.
6. Si usas un Model Context Protocol Server externo, rellena `mcp_server_url` y `mcp_server_api_key`.
7. Abre **Codexon** desde la barra lateral de Home Assistant y ejecuta `codex-login-chatgpt`.

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
