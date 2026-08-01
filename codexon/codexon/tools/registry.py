from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from tools import filesystem, homeassistant, traccar, web

ToolHandler = Callable[[Any, dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class BuiltinTool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


BUILTIN_TOOLS: dict[str, BuiltinTool] = {
    "fs_list_dir": BuiltinTool(
        name="fs_list_dir",
        description="Lista archivos y carpetas dentro de las rutas permitidas del sistema.",
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta de directorio."},
                "limit": {"type": "integer", "default": 100},
            },
            ["path"],
        ),
        handler=filesystem.fs_list_dir,
    ),
    "fs_read_file": BuiltinTool(
        name="fs_read_file",
        description="Lee un fichero de texto dentro de las rutas permitidas.",
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "max_chars": {"type": "integer", "default": 12000},
            },
            ["path"],
        ),
        handler=filesystem.fs_read_file,
    ),
    "fs_count_text": BuiltinTool(
        name="fs_count_text",
        description=(
            "Cuenta texto de forma determinista en un fichero permitido. "
            "Usala para contar lineas, apariciones de una frase o lineas exactamente iguales."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "text": {"type": "string", "description": "Texto/frase a contar. Opcional."},
                "case_sensitive": {"type": "boolean", "default": True},
            },
            ["path"],
        ),
        handler=filesystem.fs_count_text,
    ),
    "fs_write_file": BuiltinTool(
        name="fs_write_file",
        description=(
            "Crea o sobrescribe un fichero de texto dentro de las rutas permitidas. "
            "No debe usarse para secretos salvo peticion explicita."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero."},
                "content": {"type": "string", "description": "Contenido completo a escribir."},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["path", "content"],
        ),
        handler=filesystem.fs_write_file,
    ),
    "fs_delete_path": BuiltinTool(
        name="fs_delete_path",
        description=(
            "Borra un fichero o directorio dentro de las rutas permitidas. "
            "Requiere confirm=true y confirmacion explicita del usuario."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta a borrar."},
                "recursive": {"type": "boolean", "default": False},
                "confirm": {"type": "boolean", "default": False},
            },
            ["path", "confirm"],
        ),
        handler=filesystem.fs_delete_path,
    ),
    "sensor_read_file": BuiltinTool(
        name="sensor_read_file",
        description=(
            "Lee un fichero de sensores dentro de las rutas permitidas. "
            "Acepta JSON, YAML, CSV o texto key=value y devuelve datos estructurados."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero de sensores."},
                "format": {"type": "string", "enum": ["auto", "json", "yaml", "csv", "keyvalue", "text"], "default": "auto"},
                "source": {"type": "string", "description": "Nombre opcional del origen/sensor."},
            },
            ["path"],
        ),
        handler=filesystem.sensor_read_file,
    ),
    "data_write_file": BuiltinTool(
        name="data_write_file",
        description=(
            "Escribe o anexa datos a un fichero permitido. "
            "Util para exportar observaciones, decisiones, metricas o resultados web."
        ),
        parameters=object_schema(
            {
                "path": {"type": "string", "description": "Ruta del fichero destino."},
                "data": {"description": "Dato a guardar: string, objeto o lista."},
                "format": {"type": "string", "enum": ["json", "jsonl", "text"], "default": "json"},
                "append": {"type": "boolean", "default": True},
                "overwrite": {"type": "boolean", "default": False},
            },
            ["path", "data"],
        ),
        handler=filesystem.data_write_file,
    ),
    "web_fetch_url": BuiltinTool(
        name="web_fetch_url",
        description=(
            "Descarga una URL http/https publica y extrae texto basico. "
            "Usala para obtener informacion actual de paginas web o APIs publicas."
        ),
        parameters=object_schema(
            {
                "url": {"type": "string", "description": "URL publica http o https."},
                "max_chars": {"type": "integer", "default": 12000},
                "headers": {"type": "object", "description": "Cabeceras HTTP opcionales."},
            },
            ["url"],
        ),
        handler=web.web_fetch_url,
    ),
}

HOMEASSISTANT_TOOLS: dict[str, BuiltinTool] = {
    "traccar_get_location": BuiltinTool(
        name="traccar_get_location",
        description=(
            "Consulta directamente Traccar para responder donde esta la moto/Sinotrak o el movil/Samsung. "
            "Devuelve direccion, coordenadas, hora local, antiguedad, conexion, movimiento y una respuesta recomendada. "
            "Usala siempre para preguntas como 'donde esta mi moto', 'localiza mi telefono' o 'ubicacion del Samsung'; "
            "no uses otros device_tracker de Home Assistant para esos dos dispositivos."
        ),
        parameters=object_schema(
            {
                "device": {
                    "type": "string",
                    "enum": ["moto", "movil"],
                    "description": "moto corresponde a Sinotrak; movil corresponde a Samsung.",
                }
            },
            ["device"],
        ),
        handler=traccar.traccar_get_location,
    ),
    "ha_teach_entity_mapping": BuiltinTool(
        name="ha_teach_entity_mapping",
        description=(
            "Aprende, cambia, corrige, quita o lista asociaciones permanentes de entidades. "
            "Usala cuando el usuario diga que un nombre/alias corresponde a una entidad, "
            "que un dispositivo fue sustituido por otro con la misma funcion, o que una "
            "asociacion anterior debe eliminarse. Las enseñanzas prevalecen sobre el "
            "catalogo, aliases de Home Assistant y el perfil local."
        ),
        parameters=object_schema(
            {
                "operation": {
                    "type": "string",
                    "enum": ["alias", "replace", "remove", "list"],
                    "description": "alias asigna un nombre; replace sustituye una entidad; remove quita una enseñanza; list las muestra.",
                },
                "teaching_type": {
                    "type": "string",
                    "enum": ["alias", "replacement"],
                    "description": "Tipo que se elimina con operation=remove.",
                },
                "alias": {
                    "type": "string",
                    "description": "Nombre natural que se asigna o elimina.",
                },
                "old_entity_id": {
                    "type": "string",
                    "description": "Entidad anterior que se reemplaza o cuya sustitucion se elimina.",
                },
                "old_query": {
                    "type": "string",
                    "description": "Nombre natural del dispositivo anterior si no se conoce su entity_id.",
                },
                "target_entity_id": {
                    "type": "string",
                    "description": "entity_id correcto o nuevo, que se verificara en Home Assistant.",
                },
                "target_query": {
                    "type": "string",
                    "description": "Nombre natural del destino si no se conoce su entity_id.",
                },
                "domain": {
                    "type": "string",
                    "description": "Dominio opcional para desambiguar: sensor, switch, binary_sensor, etc.",
                },
                "notes": {
                    "type": "string",
                    "description": "Motivo o contexto breve de la correccion.",
                },
                "include_inactive": {
                    "type": "boolean",
                    "default": False,
                    "description": "Al listar, incluye enseñanzas eliminadas para auditoria.",
                },
            },
            ["operation"],
        ),
        handler=homeassistant.ha_teach_entity_mapping,
    ),
    "ha_get_states": BuiltinTool(
        name="ha_get_states",
        description=(
            "Lista estados actuales de Home Assistant con filtros opcionales por dominio o texto. "
            "La busqueda tolera acentos, plurales y orden natural, y devuelve match_score para elegir mejor."
        ),
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Texto a buscar en entidad, nombre, estado o atributos principales."},
                "domain": {"type": "string", "description": "Dominio opcional: sensor, switch, light, binary_sensor, etc."},
                "limit": {"type": "integer", "default": 100},
            }
        ),
        handler=homeassistant.ha_get_states,
    ),
    "ha_get_state": BuiltinTool(
        name="ha_get_state",
        description=(
            "Lee el estado completo de una entidad de Home Assistant. Puede resolver nombres amigables o aliases "
            "con entity_id/query/domain; si hay ambiguedad, devuelve error con candidatos."
        ),
        parameters=object_schema(
            {
                "entity_id": {"type": "string", "description": "Entidad o nombre natural, por ejemplo sensor.salon_temperature o Puerta cocina."},
                "query": {"type": "string", "description": "Texto alternativo para buscar si no das entity_id."},
                "domain": {"type": "string", "description": "Dominio opcional: sensor, binary_sensor, light, switch, etc."},
            }
        ),
        handler=homeassistant.ha_get_state,
    ),
    "ha_get_security_activity": BuiltinTool(
        name="ha_get_security_activity",
        description=(
            "Resume actividad/movimiento de seguridad por zona usando sensor.camera_security_test_count. "
            "Usala para preguntas como 'ha habido actividad en el terreno/huerta/casa', 'movimiento en la huerta', "
            "'actividad de seguridad en casa' o 'que sensores han saltado'. Incluye IR, camaras, presencia, radar y sensores de seguridad."
        ),
        parameters=object_schema(
            {
                "area": {"type": "string", "description": "Zona natural: terreno, huerta, casa, oficina, garrofera, olivera, cancela, almacen..."},
                "window": {"type": "string", "default": "24h", "description": "Ventana: 5min, 1h o 24h. Por defecto 24h."},
                "include_zero": {"type": "boolean", "default": False},
            }
        ),
        handler=homeassistant.ha_get_security_activity,
    ),
    "ha_search_entities": BuiltinTool(
        name="ha_search_entities",
        description=(
            "Busca entidades de Home Assistant por texto en entity_id, friendly_name, area_name, device_name, "
            "device_class, unidad o estado. Tolera acentos, plurales y nombres naturales. Conserva en query "
            "las ubicaciones que diga el usuario, por ejemplo query='salon comedor' domain='switch,light'."
        ),
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Texto de busqueda, por ejemplo temperatura, salon, puerta."},
                "domain": {"type": "string", "description": "Dominio opcional: sensor, binary_sensor, light, switch, etc."},
                "limit": {"type": "integer", "default": 20},
            },
            ["query"],
        ),
        handler=homeassistant.ha_search_entities,
    ),
    "ha_search_site_entities": BuiltinTool(
        name="ha_search_site_entities",
        description=(
            "Consulta de solo lectura para contar o listar colecciones semánticas del perfil local "
            "(roles, aliases, tags, area y kind) y devuelve sus entity_id con estado real. Úsala antes "
            "de responder cuántos dispositivos hay o cuáles son cuando el usuario emplea nombres "
            "naturales como 'grifos de la huerta'. No crea tareas ni acciona entidades."
        ),
        parameters=object_schema(
            {
                "query": {
                    "type": "string",
                    "description": "Sólo el concepto y la zona, por ejemplo 'grifos huerta'.",
                },
                "role_prefix": {
                    "type": "string",
                    "description": "Prefijo semántico opcional, por ejemplo irrigation.",
                },
                "kind": {
                    "type": "string",
                    "description": "Tipo semántico opcional, por ejemplo zone o collection.",
                },
                "limit": {"type": "integer", "default": 100},
            },
            ["query"],
        ),
        handler=homeassistant.ha_search_site_entities,
    ),
    "ha_get_services": BuiltinTool(
        name="ha_get_services",
        description="Lista servicios disponibles de Home Assistant, opcionalmente filtrados por dominio.",
        parameters=object_schema({"domain": {"type": "string", "description": "Dominio opcional: light, switch, climate, media_player, etc."}}),
        handler=homeassistant.ha_get_services,
    ),
    "ha_get_tts_media_players": BuiltinTool(
        name="ha_get_tts_media_players",
        description=(
            "Lista media_player candidatos para TTS. Descarta solo unavailable/unknown; estados off, idle, "
            "paused o playing son validos para hablar. Usala antes de cualquier peticion de hablar, decir, "
            "anunciar o avisar por voz."
        ),
        parameters=object_schema(
            {
                "query": {"type": "string", "description": "Filtro opcional por nombre o entity_id."},
                "limit": {"type": "integer", "default": 100},
            }
        ),
        handler=homeassistant.ha_get_tts_media_players,
    ),
    "ha_call_service": BuiltinTool(
        name="ha_call_service",
        description=(
            "Ejecuta un servicio generico de Home Assistant. "
            "Requiere confirm=true si cambia estado o si la configuracion exige confirmacion. "
            "Para cualquier peticion de hablar, decir, anunciar, avisar por voz o reproducir un mensaje hablado, "
            "usa TTS en un media_player. Interpreta frases como 'di hola por algun altavoz' como voz, busca antes "
            "destinos audibles con ha_get_tts_media_players(query='', limit=100), ofrece opciones si no hay destino claro, y usa exactamente "
            "tts.google_translate_say con cache=false, language=es, entity_id y message. TTS no requiere "
            "confirmacion extra cuando el usuario pidio hablar. Si la llamada acaba bien, di que el mensaje fue enviado; "
            "no afirmes que se oyo o se reprodujo porque la API no lo confirma."
        ),
        parameters=object_schema(
            {
                "domain": {"type": "string", "description": "Dominio del servicio, por ejemplo light o switch."},
                "service": {"type": "string", "description": "Nombre del servicio, por ejemplo turn_on, turn_off, toggle."},
                "service_data": {"type": "object", "description": "Datos del servicio."},
                "target": {"type": "object", "description": "Target HA, por ejemplo {entity_id: light.salon}."},
                "confirm": {"type": "boolean", "default": False},
            },
            ["domain", "service"],
        ),
        handler=homeassistant.ha_call_service,
    ),
    "ha_press_entity_interval": BuiltinTool(
        name="ha_press_entity_interval",
        description=(
            "Pulsa una accion de Home Assistant durante un intervalo y vuelve a pulsarla al final. "
            "Usala para sirenas/avisadores catalogados como acciones pulsables cuando el usuario pida activarlas "
            "durante N segundos o minutos. Para button/input_button llama press; para switch llama toggle; siempre requiere confirm=true."
        ),
        parameters=object_schema(
            {
                "entity_id": {"type": "string", "description": "Entidad concreta si se conoce."},
                "query": {"type": "string", "description": "Nombre natural si hay que resolver la entidad."},
                "domain": {"type": "string", "description": "Dominio opcional para resolver, por ejemplo button,switch."},
                "duration_seconds": {"type": "number", "description": "Duracion del intervalo en segundos."},
                "confirm": {"type": "boolean", "default": False},
            },
            ["duration_seconds"],
        ),
        handler=homeassistant.ha_press_entity_interval,
    ),
    "ha_send_mobile_alert": BuiltinTool(
        name="ha_send_mobile_alert",
        description=(
            "Envia una alerta/notificacion al movil. Usala cuando el usuario pida llamar, avisar, mandar una alerta "
            "o notificar algo. Por defecto usa notify/mobile_app_sm_a566b, sube el volumen del alarm_stream, hace que "
            "el movil diga el mensaje mediante TTS y envia una notificacion critica con sonido. Usa speak=false para "
            "omitir la voz y critical=false para una notificacion normal."
        ),
        parameters=object_schema(
            {
                "message": {"type": "string", "description": "Mensaje a notificar. Por defecto: Han llamado a la puerta."},
                "title": {"type": "string", "description": "Titulo opcional de la notificacion."},
                "notify": {"type": "string", "default": "notify/mobile_app_sm_a566b", "description": "Servicio notify, por ejemplo notify/mobile_app_sm_a566b."},
                "volume": {"type": "integer", "default": 100},
                "media_stream": {"type": "string", "default": "alarm_stream"},
                "speak": {"type": "boolean", "default": True, "description": "Hace que el movil diga el mensaje mediante TTS."},
                "critical": {"type": "boolean", "default": True, "description": "Usa el canal de alarma y sonido critico."},
                "data": {"type": "object", "description": "Datos adicionales opcionales para la notificacion real."},
            }
        ),
        handler=homeassistant.ha_send_mobile_alert,
    ),
    "ha_render_template": BuiltinTool(
        name="ha_render_template",
        description="Renderiza una plantilla Jinja de Home Assistant para consultas avanzadas de estado.",
        parameters=object_schema({"template": {"type": "string", "description": "Plantilla Jinja de Home Assistant."}}, ["template"]),
        handler=homeassistant.ha_render_template,
    ),
    "ha_get_events": BuiltinTool(
        name="ha_get_events",
        description="Lista tipos de eventos disponibles en Home Assistant.",
        parameters=object_schema({}),
        handler=homeassistant.ha_get_events,
    ),
    "ha_get_error_log": BuiltinTool(
        name="ha_get_error_log",
        description="Lee el final del log de errores de Home Assistant.",
        parameters=object_schema({"max_chars": {"type": "integer", "default": 12000}}),
        handler=homeassistant.ha_get_error_log,
    ),
    "ha_get_history": BuiltinTool(
        name="ha_get_history",
        description=(
            "Consulta historico de Home Assistant. Si no sabes la entidad, usa query/domain/device_class "
            "para buscar varias entidades candidatas, por ejemplo query=temperatura, domain=sensor, device_class=temperature."
        ),
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin en ISO 8601."},
                "entity_id": {"type": "string", "description": "Entidad concreta de Home Assistant. Opcional si usas query/domain."},
                "query": {"type": "string", "description": "Texto para buscar entidades candidatas, por ejemplo temperatura."},
                "domain": {"type": "string", "description": "Dominio opcional para buscar candidatos, por defecto sensor."},
                "device_class": {"type": "string", "description": "device_class opcional, por ejemplo temperature."},
                "limit": {"type": "integer", "default": 12},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_get_history,
    ),
    "ha_get_history_around_time": BuiltinTool(
        name="ha_get_history_around_time",
        description=(
            "Consulta el valor historico de una entidad alrededor de una hora local durante varios dias. "
            "Usala para preguntas como 'temperatura alrededor de las 10am de los ultimos 7 dias'."
        ),
        parameters=object_schema(
            {
                "entity_id": {"type": "string", "description": "Entidad concreta de Home Assistant."},
                "local_time": {"type": "string", "description": "Hora local objetivo, por ejemplo 10:00 o 10am.", "default": "10:00"},
                "days": {"type": "integer", "default": 7},
                "window_minutes": {"type": "integer", "default": 30},
                "timezone": {"type": "string", "default": "Europe/Madrid"},
                "end_date": {"type": "string", "description": "Fecha final local YYYY-MM-DD. Opcional."},
                "query": {"type": "string", "description": "Texto para buscar entidad si no das entity_id."},
                "domain": {"type": "string", "default": "sensor"},
                "device_class": {"type": "string", "description": "device_class opcional, por ejemplo temperature."},
            },
            ["local_time"],
        ),
        handler=homeassistant.ha_get_history_around_time,
    ),
    "ha_aggregate_numeric_history": BuiltinTool(
        name="ha_aggregate_numeric_history",
        description=(
            "Agrupa el historico numerico de cualquier sensor por hora, dia o semana y calcula min, max, media, "
            "primero, ultimo o delta. Usala para comparar periodos: que dia se consumio mas agua/energia, "
            "que hora tuvo mayor potencia o que semana tuvo la temperatura media mas alta. En contadores que "
            "se reinician cada dia, usa group_by=day y aggregation=max."
        ),
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio del periodo en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin exclusivo del periodo en ISO 8601."},
                "entity_id": {"type": "string", "description": "Sensor concreto. Opcional si usas query."},
                "query": {"type": "string", "description": "Texto para buscar el sensor si no das entity_id."},
                "domain": {"type": "string", "default": "sensor"},
                "device_class": {"type": "string", "description": "Clase opcional, por ejemplo water, energy o temperature."},
                "group_by": {"type": "string", "enum": ["hour", "day", "week"], "default": "day"},
                "aggregation": {
                    "type": "string",
                    "enum": ["min", "max", "mean", "first", "last", "delta"],
                    "default": "max",
                },
                "timezone": {"type": "string", "default": "Europe/Madrid"},
                "exclude_start_state": {
                    "type": "boolean",
                    "default": True,
                    "description": "Ignora el estado sintetico que HA inserta exactamente al inicio del historico.",
                },
                "limit": {"type": "integer", "default": 12},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_aggregate_numeric_history,
    ),
    "ha_measure_numeric_during_state": BuiltinTool(
        name="ha_measure_numeric_during_state",
        description=(
            "Calcula cuanto registro un contador numerico solamente durante los intervalos en que otra entidad "
            "estuvo en un estado concreto. Sirve para atribuir consumo a una valvula, zona, maquina o interruptor: "
            "por ejemplo litros medidos por un caudalimetro mientras un grifo estuvo on. Cruza ambos historicos, "
            "suma incrementos positivos y devuelve total, intervalos y cobertura."
        ),
        parameters=object_schema(
            {
                "activity_entity_id": {
                    "type": "string",
                    "description": "Entidad cuya actividad delimita el consumo, por ejemplo switch de una valvula.",
                },
                "measurement_entity_id": {
                    "type": "string",
                    "description": "Sensor numerico acumulativo o temporal que mide el consumo.",
                },
                "start_time": {"type": "string", "description": "Inicio inclusivo en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin exclusivo en ISO 8601."},
                "active_state": {"type": "string", "default": "on"},
            },
            ["activity_entity_id", "measurement_entity_id", "start_time", "end_time"],
        ),
        handler=homeassistant.ha_measure_numeric_during_state,
    ),
    "ha_get_long_term_statistics": BuiltinTool(
        name="ha_get_long_term_statistics",
        description=(
            "Consulta estadisticas de largo plazo de cualquier sensor numerico. Usala cuando el recorder ya no "
            "conserve estados crudos, por ejemplo para consumo de energia o agua de meses anteriores."
        ),
        parameters=object_schema(
            {
                "entity_id": {"type": "string", "description": "ID estadistico, normalmente el entity_id del sensor."},
                "start_time": {"type": "string", "description": "Inicio inclusivo en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin exclusivo en ISO 8601."},
            },
            ["entity_id", "start_time", "end_time"],
        ),
        handler=homeassistant.ha_get_long_term_statistics,
    ),
    "ha_get_pvpc_cheapest_hours": BuiltinTool(
        name="ha_get_pvpc_cheapest_hours",
        description=(
            "Consulta el precio PVPC de electricidad en sensor.pvpc_dh y devuelve las horas mas baratas del kWh/consumo/luz. "
            "Usala cuando el usuario pregunte por precio de la luz, consumo barato, kWh barato/caro, o mejores horas para consumir electricidad."
        ),
        parameters=object_schema(
            {
                "sensor_entity_id": {"type": "string", "default": "sensor.pvpc_dh", "description": "Sensor PVPC. Por defecto sensor.pvpc_dh."},
                "limit": {"type": "integer", "default": 5, "description": "Numero de horas baratas a devolver."},
                "include_all_hours": {"type": "boolean", "default": False, "description": "Incluir las 24 horas con precio."},
                "timezone": {"type": "string", "default": "Europe/Madrid"},
            }
        ),
        handler=homeassistant.ha_get_pvpc_cheapest_hours,
    ),
    "ha_plan_ac_pvpc_budget": BuiltinTool(
        name="ha_plan_ac_pvpc_budget",
        description=(
            "Calcula un plan candidato para usar el aire acondicionado Brokton dentro de un presupuesto en euros "
            "con precios PVPC. No agenda ni ejecuta: devuelve horas baratas, coste estimado, potencia asumida y "
            "requiere explicar el plan y pedir confirmacion/correccion antes de crear tareas."
        ),
        parameters=object_schema(
            {
                "budget_eur": {"type": "number", "default": 1.0, "description": "Presupuesto maximo en euros."},
                "sensor_entity_id": {"type": "string", "default": "sensor.pvpc_dh"},
                "climate_entity_id": {"type": "string", "default": "input_boolean.brokton_ac_dp1_switch"},
                "power_sensor_entity_id": {"type": "string", "default": "sensor.powcasa_enchufes_powcasa_enchufes_power"},
                "ac_power_kw": {"type": "number", "default": 1.2, "description": "Potencia media estimada del AC en kW."},
                "start_hour": {"type": "integer", "description": "Hora local de inicio, 0-23."},
                "end_hour": {"type": "integer", "description": "Hora local final exclusiva, 1-24."},
                "only_future": {"type": "boolean", "default": True},
                "timezone": {"type": "string", "default": "Europe/Madrid"},
            },
            ["budget_eur"],
        ),
        handler=homeassistant.ha_plan_ac_pvpc_budget,
    ),
    "ha_count_state_transitions": BuiltinTool(
        name="ha_count_state_transitions",
        description=(
            "Cuenta transiciones de estado en historico de Home Assistant para cualquier entidad o lista de entidades. "
            "Usala para preguntas como cuantas veces se activo un sensor, cuantas aperturas hubo, o cuantos cambios off->on/on->off ocurrieron."
        ),
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin en ISO 8601 opcional."},
                "entity_id": {"type": "string", "description": "Entidad concreta de Home Assistant."},
                "entity_ids": {"type": "array", "items": {"type": "string"}, "description": "Lista opcional de entidades."},
                "query": {"type": "string", "description": "Texto para buscar entidades si no das entity_id/entity_ids."},
                "domain": {"type": "string", "description": "Dominio opcional, por ejemplo binary_sensor."},
                "device_class": {"type": "string", "description": "device_class opcional."},
                "from_state": {"type": "string", "default": "off", "description": "Estado origen. Por defecto off."},
                "to_state": {"type": "string", "default": "on", "description": "Estado destino. Por defecto on."},
                "limit": {"type": "integer", "default": 12},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_count_state_transitions,
    ),
    "ha_get_logbook": BuiltinTool(
        name="ha_get_logbook",
        description="Consulta el logbook historico de Home Assistant para aperturas, movimiento, alarmas o cambios de estado.",
        parameters=object_schema(
            {
                "start_time": {"type": "string", "description": "Inicio en ISO 8601."},
                "end_time": {"type": "string", "description": "Fin en ISO 8601."},
                "entity_id": {"type": "string", "description": "Entidad opcional."},
            },
            ["start_time"],
        ),
        handler=homeassistant.ha_get_logbook,
    ),
}


def builtin_tool_names(*, include_homeassistant: bool = False) -> list[str]:
    names = list(BUILTIN_TOOLS)
    if include_homeassistant:
        names.extend(HOMEASSISTANT_TOOLS)
    return names


def builtin_tool_schemas(*, include_homeassistant: bool = False) -> list[dict[str, Any]]:
    tools = list(BUILTIN_TOOLS.values())
    if include_homeassistant:
        tools.extend(HOMEASSISTANT_TOOLS.values())
    return [tool.openai_schema() for tool in tools]


async def call_builtin_tool(context: Any, name: str, args: dict[str, Any]) -> str:
    tool = BUILTIN_TOOLS.get(name) or HOMEASSISTANT_TOOLS.get(name)
    if tool is None:
        raise ValueError(f"Herramienta interna desconocida: {name}")
    return await tool.handler(context, args)
