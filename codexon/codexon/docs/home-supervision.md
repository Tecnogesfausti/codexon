# Supervisión de seguridad y consumos

El servicio `HomeMonitor` revisa cada 60 segundos las entidades configuradas en `energy.circuit.*`, `water.monitor` y `security.visual.*`. Resumen muestra disponibilidad, fallos, último evento y última revisión. Agua y electricidad muestra medidas y patrones; las pestañas se ajustan al ancho del dispositivo. El panel actualiza estos datos cada 30 segundos.

## Seguridad

El supervisor recibe eventos del bus de HA y, si `frigate_base_url` está configurado, consulta la API local de Frigate cada 10 segundos. `frigate_cameras` son identificadores de Frigate, no nombres de entidades de HA. El cliente no envía el token de HA a Frigate y no sigue redirecciones. Los eventos de ambas rutas se deduplican por identificador.

`motion_entities` permite revisar movimiento cuando el detector nativo de personas no esté disponible; un movimiento nunca se etiqueta como una detección nativa de persona. La disponibilidad de la cámara no prueba que el detector funcione. La comprobación de instalación procesa fotos reales y aparece separada de las detecciones; no simula estados en HA ni una intrusión.

Para Frigate se busca primero la fotografía del evento, y se usa su miniatura si no existe la fotografía completa. La correspondencia temporal se verifica con las marcas del evento. Una foto actual sin personas no refuta una detección pasada. Los fotogramas se conservan 15 días por defecto en una carpeta privada.

Se puede configurar `review_model` y `daily_escalation_limit` (3 por defecto). Las propuestas de falsa detección y las imágenes ambiguas con evidencia suficiente reciben una segunda revisión dentro del límite diario total. Un desacuerdo, un error o un presupuesto agotado conserva la incertidumbre. Ambos modelos comparten evidencia: no equivalen a dos cámaras independientes.

Las etiquetas humanas alimentan métricas por cámara y día/noche. Las causas descritas por el modelo son propuestas, no causas verificadas automáticamente. Las comprobaciones de instalación no cuentan como ejemplos de precisión. El modo de observación conserva las alarmas y protecciones existentes; las revisiones no controlan sirenas ni válvulas.

## Agua

`water.monitor.entity_id` debe ser el contador acumulado real en litros, no un contador temporal que se pone a cero al iniciar un riego. `calibration_entity` permite expresar la resolución física. `availability_entities` y `require_event_tracking` impiden confirmar cobertura cuando falla el dispositivo o el seguimiento de cambios de relés.

Se registran muestras de un minuto y los cambios de relés del bus de HA. Así, un riego breve entre dos lecturas no se confunde con agua pasando mientras todos los relés estaban apagados. Los reinicios de contador y las interrupciones de muestreo no se convierten en consumo.

Hay detecciones de caudal alto, avances repetidos con todos los relés apagados y fugas lentas observables durante una hora o más. Los umbrales son configurables. El estado eléctrico de un relé no demuestra el cierre físico de una válvula. No se promete detectar una fuga inferior a la resolución del contador antes de que se acumulen pulsos suficientes.

Se aprenden sesiones de una sola zona, descartando simultaneidad, resets y discontinuidades. Los valores son aproximaciones muestreadas cada minuto; no incluyen necesariamente todo el volumen anterior a la primera lectura de la sesión. Una zona necesita al menos cinco sesiones válidas para comparar su caudal. La supervisión registra hallazgos sin modificar las protecciones hidráulicas de AppDaemon/ESPHome.

## Electricidad

Cada `energy.circuit.*` declara potencia en W, contador de energía en kWh y contador diario en kWh. Solo se agregan circuitos independientes configurados; no se añade de nuevo un sensor que ya suma esos circuitos. No se atribuye el consumo de un circuito mixto a un aparato concreto.

Se consultan 28 días de estadísticas horarias de Recorder al inicio y cada hora. Se usa `change`, que contempla reinicios de contadores. Los periodos incompletos, cambios negativos, valores no finitos y valores físicamente incompatibles se descartan. Los patrones usan la zona horaria del perfil.

El último periodo se compara con datos anteriores, sin incluirlo en su propia línea de base. Se calculan medianas por hora y día de semana, dispersión robusta y desviaciones; la comparación por hora separa días laborables y fin de semana. Sin suficientes muestras se indica aprendizaje; con histórico atrasado se indica la limitación.

Los ahorros son escenarios explícitos: reducir un 20% de la base nocturna durante cinco horas al día. Los euros utilizan el precio actual, no un precio futuro garantizado. Antes de apagar cargas hay que identificar cuáles pueden desconectarse.

## Eficiencia y diagnóstico

La observación periódica consume el resultado medido de los supervisores y agrupa informes locales cada seis horas o cuando cambia un riesgo. No llama a `ask`, clasificación ni extracción de memorias cada cinco minutos. La cifra de llamadas evitadas compara cada ciclo válido con las dos llamadas del funcionamiento anterior; no es un descuento de facturación confirmado.

API: `GET api/home-monitor`, `GET api/security-visual`, `POST api/security-visual/check`, `PATCH api/security-visual/{id}/label`. Las rutas de imágenes son relativas para conservar el acceso a través de Ingress.

## Otra instalación con solo electricidad

Copia `site.electricity.example.yaml` a `/addon_config/site.yaml` y sustituye los sensores de ejemplo por los identificadores de ese HA. Configura la zona horaria IANA en `timezone`. No copies el perfil privado, la base de datos ni las tareas del HA anterior: podrían referirse a sus dispositivos.

Sin `water.monitor` no se analiza agua; sin `security.visual.*` habilitados no se arranca el supervisor de imágenes ni se hacen llamadas a IA para cámaras. AppDaemon es opcional y solo se utiliza para los ejemplos de seguridad. El agente, las tareas y el portal siguen disponibles. El aprendiz eléctrico requiere roles `energy.circuit.*`, potencia en W y contadores en kWh; sin estadísticas de Recorder queda en aprendizaje. El contador diario y el precio son opcionales: si faltan, sus medidas o cálculos se indican como desconocidos. Configura circuitos independientes para evitar sumar dos veces el contador general y sus subcircuitos.
