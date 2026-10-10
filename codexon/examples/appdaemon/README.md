# Integración opcional con AppDaemon

Estos módulos conservan la integración de seguridad, contadores de presencia y semáforos del floorplan. No se instalan ni se arrancan al instalar Codexon. En un HA sin cámaras o presencia no hacen falta.

Para utilizarlos, copia los módulos a los apps de AppDaemon y adapta `apps.example.yaml`. Crea los helpers y el servicio de móvil que declares; sustituye todos los identificadores de ejemplo. Los nombres de cámaras deben coincidir con los roles `security.visual.<nombre>` del perfil local. El puente Frigate usa `sensor.frigate_event` con `attributes.after`; adapta el sensor MQTT a tu instalación. Las cámaras deben tener detección de personas. Los módulos de contadores y floorplan tienen configuración independiente y no se activan con este ejemplo.

La prueba no acciona dispositivos; fuera del simulacro solo se ejecutan las acciones que configures explícitamente. AppDaemon y Codexon conservan presupuestos, tiempos máximos y sus rutas de fallo. El desarmado detiene ambos modos y sus actuaciones. No copies la configuración privada de otra vivienda.
