# Piloto visual de seguridad

El piloto consume los eventos de Home Assistant a través del WebSocket existente. Registra episodios para las cámaras habilitadas en el perfil local y revisa imágenes con el modelo concreto seleccionado en Análisis de imágenes. Las fotos se envían a ese proveedor de OpenRouter. No hereda las herramientas del agente conversacional, no envía alertas de intrusión ni opera dispositivos. El código puede crear únicamente avisos de mantenimiento en Home Assistant si se retira el modelo de IA. Solo admite modo `shadow`; no puede activarse el filtrado mediante configuración.

Ejemplo de perfil (adaptar los identificadores a cada instalación):

```yaml
version: 1
retired_entities:
  - binary_sensor.sensor_retired
roles:
  security.visual.casa:
    entity_id: camera.casa
    enabled: true
    mode: shadow
    presence_entities: [binary_sensor.casa_person]
    frigate_cameras: [casa]
    frigate_event_sensor: sensor.frigate_event
    daily_call_limit: 20
    cooldown_seconds: 30
    max_event_age_seconds: 30
```

Los detectores Tapo generan dos capturas actuales separadas un segundo. Su hora de captura original es desconocida: una imagen vacía no permite clasificar el episodio como falso positivo. Frigate usa el ID y la hora de su snapshot; las actualizaciones de la misma captura no repiten el modelo y una captura mejor permite como máximo una segunda revisión. Tapo y Frigate próximos en la misma cámara se agrupan, sin tratarlos como evidencias independientes.

Resultados: persona visible, falsa detección sustentada o inconcluso. El código valida el ID, el contrato, la calidad y la correspondencia temporal. Los errores, límites, reinicios o imágenes no disponibles conservan la incertidumbre. La vía actual de AppDaemon sigue funcionando independientemente.

La pestaña **Seguridad visual** muestra los casos y permite etiquetarlos como persona real, falsa detección, presencia legítima o no determinable. Una etiqueta no modifica la conclusión original ni las reglas. La métrica de personas reales que se habrían filtrado permite detectar un error especialmente relevante. No equivale a una tasa de precisión validada: solo cuenta episodios recibidos y etiquetados.

Las imágenes se guardan con permisos 0600 dentro de `security_visual_images`, junto a la base de datos, y caducan a los 15 días por defecto. El plazo se puede cambiar entre 1 y 90 días con `input_number.codexon_imagenes_conservacion_dias` o desde el control «Conservar imágenes (días)» de Seguridad visual. El worker borra las fotos caducadas cada hora y al cambiar el plazo; «Guardar y borrar antiguas» aplica inmediatamente la limpieza. Solo se eliminan archivos de imagen propios de esa carpeta; se conservan los análisis y las etiquetas. Las imágenes desaparecidas se muestran como caducadas, sin enlaces rotos.

La vista muestra hoy y ayer por defecto, usando la zona horaria del sitio. «Desde», «Hasta» y «Mostrar imágenes» permiten consultar otras fechas; ambos días se incluyen. La consulta trabaja con los registros guardados, sin llamar a la IA. Los controles se vinculan con `input_datetime.codexon_imagenes_desde`, `input_datetime.codexon_imagenes_hasta` e `input_button.codexon_mostrar_imagenes`. «Últimos 2 días» recupera la vista inicial. El número de imágenes y su tamaño aparecen en «Almacenamiento de imágenes».

En esta instalación se incluyen también los archivos automáticos `security_<cámara>_<fecha>_<hora>.jpg` de los avisos de CameraSecurity en `/media`. La configuración explícita `security_visual.legacy_archive` limita el borrado a las cámaras configuradas, sin seguir enlaces simbólicos ni borrar fotografías manuales. Estas fotos de avisos se cuentan por separado y comparten el plazo de conservación; la consulta por fechas muestra los episodios analizados por Codexon.

Los episodios y revisiones se retienen 90 días. Las rutas HTTP sirven exclusivamente archivos asociados a una revisión y dentro de esa carpeta. El portal mantiene el control de acceso del ingress existente. Las copias de seguridad generales pueden conservar imágenes más tiempo: la retención del worker no modifica esas copias. Frigate conserva sus originales por separado según su propia configuración.

Límites del piloto: no reconstruye eventos perdidos durante una desconexión, no verifica identidad ni autorización por apariencia y no controla todavía las notificaciones de AppDaemon. No se puede estimar una mejora de falsos positivos hasta revisar episodios reales diurnos/nocturnos y casos controlados. El aprendizaje de consumos de agua y electricidad sigue siendo una fase posterior del plan.

Para pausar el piloto, poner `enabled: false` en sus roles y reiniciar el servicio de Codexon. Los casos pendientes de cámaras deshabilitadas quedan sin revisión hasta una futura habilitación; no afectan a las alertas existentes.

Respaldo de IA: ante un fallo del modelo visual se intenta Gemini 2.5 Flash Lite con los mismos fotogramas, sin volver a capturar fotos. Cada intento reserva su propia revisión y consume el límite diario, incluso si falla. Tras un fallo se utiliza Gemini durante 10 minutos antes de volver a probar el principal. Las validaciones y la segunda opinión para propuestas de falsa detección se conservan. Si ambos modelos fallan o no queda presupuesto, el episodio queda inconcluso y AppDaemon conserva sus avisos.

El catálogo público de OpenRouter se comprueba al iniciar, al cambiar la selección y cada 10 minutos. Una consulta válida que ya no incluya el modelo seleccionado activa Gemini hasta que el catálogo confirme su recuperación y crea una notificación persistente de mantenimiento en HA (`codexon_security_visual_model_removed`). Un catálogo vacío o inaccesible no demuestra una retirada. Un fallo temporal del proveedor tampoco genera un aviso de modelo inexistente. Se envía una notificación por retirada, se usa siempre el mismo ID y se reintenta a los 5 minutos si HA estaba desconectado. El panel muestra los cambios al respaldo, los errores de la última revisión y los avisos de mantenimiento pendientes o enviados. No se cambian automáticamente las preferencias generales de modelo.


## Revisión automática según el armado

El role `security.visual_policy` habilita una política determinista mediante `entity_id` (armado total), `night_arm_entity` y `night_cameras`. El perfil instalado usa `switch.sonar_casa_alarma`, `input_boolean.seguridad_noche` y solamente `camara_casa`, siguiendo la cobertura de CameraSecurity. Las seis cámaras se revisan con armado total. La detección local, las grabaciones y AppDaemon siguen funcionando por separado. Este cambio no habilita el filtrado de avisos: continúa en shadow.

Sin armado, no se capturan fotos ni se llama a la IA para eventos nuevos. Los eventos pendientes se comprueban antes de la captura. El armado se consulta al iniciar y cada 30 s, y recibe transiciones por WebSocket. Cada episodio conserva la decisión de armado en el momento de la detección (o el fotograma actualizado de un evento Frigate en curso), por lo que desarmar después no elimina un evento protegido. Si se pierde HA, faltan estados, hay una ventana sin cubrir o no se conoce el estado histórico, se conserva la revisión con una incidencia visible. Al armar, una presencia Tapo ya activa puede iniciar una revisión. Las comprobaciones explícitas con botón siguen disponibles desarmado y mantienen los límites de llamadas. Los sucesos desarmados descartados no generan imágenes ni tarjetas inconclusas.


## Filtro conservador activo de avisos Frigate

`security.visual_filter.enabled` y `CameraSecurity.visual_filter_enabled` activan el puente por eventos HA `codexon_security_review_requested`, `codexon_security_review_result` y `codexon_security_filter_outcome`. AppDaemon conserva un temporizador independiente de 15 s. La solicitud lleva un identificador aleatorio, cámara, ID del evento y fotograma solicitado. Una respuesta solo puede descartar ese evento pendiente, antes de su plazo.

El descarte exige dos modelos distintos, respuestas válidas sin error, cero personas, calidad adecuada, correspondencia temporal y el mismo hash de la foto Frigate del evento. Se exige snapshot fresco (hasta 10 s antes de la solicitud); thumbnails, fotos Tapo actuales, otra cámara, otro evento, una persona confirmada por cualquiera de las revisiones o una etiqueta humana de persona/presencia legítima conservan el aviso. Se respeta el presupuesto de llamadas; agotarlo conserva avisos.

Tapo y sensores físicos mantienen avisos inmediatos. Si Tapo avisa durante la espera de Frigate, su aviso tiene prioridad y el resultado IA no lo revoca. En tiempo agotado, fallo o duda, AppDaemon avisa normalmente; la respuesta tardía no modifica ese aviso. Los disuasores nocturnos asociados exclusivamente al aviso Frigate siguen su resolución, mientras los físicos mantienen su ruta independiente. El panel muestra descartes reales por separado de la evaluación del modelo. Reinicios conservan las solicitudes backend para auditoría; AppDaemon intenta emitir pendientes antes de recargar código.


## Simulacro visual de dos minutos

La integración opcional de AppDaemon en `examples/appdaemon/CameraSecurity.py` abre una ventana fija de 120 segundos desde `input_button.simulacro` o el botón del portal. Funciona con las alarmas desarmadas. Anuncia por TTS cada sensor o cámara una vez por prueba; Tapo y Frigate se agrupan. En cámaras, la primera voz anuncia detección y comprobación pendiente. Codexon analiza las imágenes reales y envía el resultado por `codexon_security_simulacro_visual_result` para anunciar persona confirmada, falsa detección descartada o imagen sin verificar. Descartar requiere dos modelos diferentes sobre el fotograma del evento y ausencia de personas con alternativa visual. Los resultados pendientes pueden llegar hasta un minuto después de finalizar; cancelar o iniciar otra prueba invalida los resultados antiguos.

La revisión conserva los límites diarios y los intervalos entre llamadas. Agotar presupuesto, fallar la imagen o tener dudas nunca declara una falsa detección. La prueba registra el contexto de luz y el tiempo informado por HA; las etiquetas humanas permiten comparar condiciones. Los inconclusos se ocultan en la lista. Una persona omitida por ambos detectores no produce un episodio revisable y debe anotarse en el recorrido de prueba.

CameraSecurity no inicia acciones físicas durante la ventana. El desarmado detiene las actuaciones de seguridad de ambos modos; cancelar detiene la alarma actual y el simulacro. La voz usa el volumen máximo del canal Android de alarmas y `command_stop_tts` al cancelar. Las automatizaciones ajenas a CameraSecurity conservan su funcionamiento. La instalación de Codexon por sí sola no instala AppDaemon, sus helpers ni estas acciones.
