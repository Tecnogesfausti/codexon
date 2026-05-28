# Changelog

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
