# Home Assistant Codex Agent

Repositorio de add-on para ejecutar Codex CLI dentro de Home Assistant con permisos amplios sobre el sistema supervisado.

> Este add-on está pensado para uso propio y entornos controlados. Con `full_access`, montajes de escritura y tokens persistentes, cualquier sesión de Codex puede modificar configuración, add-ons, ficheros compartidos y otros servicios accesibles desde el Supervisor.

## Add-on incluido

- `codex_agent`: entorno Debian con Codex CLI, Node.js, Python, herramientas de desarrollo, cliente MCP básico y utilidades para operar contra Home Assistant/Supervisor.

## Instalación local

1. Copia este repositorio en `/addons/ha_codex_agent` dentro de Home Assistant OS/Supervised, o añádelo como repositorio de add-ons si lo publicas en GitHub.
2. En Home Assistant, ve a **Settings > Add-ons > Add-on Store > Check for updates**.
3. Instala **Codex Agent**.
4. Configura como mínimo `openai_api_key` o usa autenticación interactiva de Codex si tu entorno lo permite.
5. Define `ha_long_lived_token` si quieres que el agente use un token largo de Home Assistant además del `SUPERVISOR_TOKEN` automático del add-on.

## Seguridad operativa

- Mantén `require_confirmation: true` mientras lo pruebes.
- No expongas el puerto SSH/Web a Internet.
- Usa un token de Home Assistant dedicado, con rotación manual y revocación fácil.
- Revisa los cambios antes de reiniciar Home Assistant, ESPHome, AppDaemon u otros add-ons.
