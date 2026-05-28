# Changelog

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
