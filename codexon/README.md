# Codexon

Add-on experimental para ejecutar Codex CLI dentro de Home Assistant con acceso amplio a configuración, add-ons y API de Supervisor.

Consulta `DOCS.md` antes de activarlo: está diseñado para administración avanzada y no para exposición pública.

Desde la versión 0.3.20 basta ejecutar `codex` desde la terminal web: la consola abre el workspace configurado con `sandbox_mode = "workspace-write"`, `approval_policy = "on-request"` y `sandbox_workspace_write.network_access = true`, incluso con una configuración de proyecto antigua. Esto permite consultar la API de Home Assistant y acceder a los dispositivos ESPHome desde el prompt, con aprobación disponible para permisos adicionales. El arranque también genera estos valores en la configuración de usuario. Tras actualizar, sal de Codex con `/quit` y de la consola con `exit`, y vuelve a abrir la terminal web para cargar la función nueva. Una política administrada puede limitar los permisos; argumentos explícitos posteriores pueden cambiar los valores de la función.

La terminal incluye también OpenCode CLI. Ejecuta `opencode` para iniciarlo y configura tu proveedor de modelos con `/connect`.

El portal incluye una pestaña **Archivos y OpenCode** para explorar las carpetas permitidas, subir y descargar ficheros y trabajar con ellos desde un prompt.
OpenCode puede usar Bash y escribir archivos desde esa pestaña. La imagen incluye Pandoc, LaTeX y utilidades de PDF (`html-to-pdf`, `pdftotext`, `qpdf`, `gs`).


## Multimedia y seguridad visual (0.3.21)

La consola permite adjuntar imágenes, audio y vídeo y consultar el estado de los trabajos y su consumo. FFmpeg procesa los archivos multimedia en el complemento. El proveedor de análisis se selecciona explícitamente: las opciones basadas en ChatGPT utilizan la sesión de Codex, y un fallo no activa automáticamente una API de pago.

La revisión visual conserva la captura original del evento Frigate y distingue los recortes de los fotogramas de otros momentos. Los episodios inconclusos se muestran en Seguridad visual para poder revisar errores y fotografías. Los avisos con fotos de evento se incluyen en la [integración opcional de AppDaemon](examples/appdaemon/README.md); no se instalan ni alteran las automatizaciones de otras viviendas al actualizar Codexon.
