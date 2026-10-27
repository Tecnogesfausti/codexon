import os
from pathlib import Path
from SecurityEventImage import event_image
import time
import uuid
from datetime import datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass


class CameraSecurity(hass.Hass):
    """Collect camera evidence, notify, and run configured deterrent actions."""

    SIMULACRO_SECONDS = 120

    def initialize(self):
        self.arm_switch = self.args["arm_switch"]
        self.night_arm_switch = self.args.get("night_arm_switch")
        self.night_sensors = set(self.args.get("night_sensors", []))
        self.night_deterrents = self.args.get("night_deterrents", [])
        self.night_ack_entities = {
            action["entity"] for action in self.night_deterrents
        }
        self.night_escalation_deterrents = self.args.get(
            "night_escalation_deterrents", []
        )
        self.alarm_cover_triggers = set(self.args.get("alarm_cover_triggers", []))
        self.alarm_covers = self.args.get("alarm_covers", [])
        self.alarm_cover_delay = int(self.args.get("alarm_cover_delay", 20))
        self.alarm_cover_timer = None
        self.active_alarm_covers = set()
        self.night_escalation_timer = None
        self.notify_service = self.args["notify"]
        self.snapshot_dir = self.args.get("snapshot_dir", "/media")
        self.cooldown = int(self.args.get("cooldown", 60))
        self.person_cooldown = max(self.cooldown, int(self.args.get("person_cooldown", self.cooldown)))
        self.motion_alerts_enabled = self.args.get("motion_alerts_enabled", True) is True
        self.frigate_event_sensor = self.args.get("frigate_event_sensor")
        self.nest_player = self.args.get("nest_player")
        self.visual_filter_enabled = self.args.get("visual_filter_enabled", False) is True
        self.visual_filter_wait = max(5, min(int(self.args.get("visual_filter_wait_seconds", 15)), 30))
        self.visual_pending = {}
        self.visual_counts = {"suppressed": 0, "notified": 0, "timeout": 0, "already_notified": 0, "disarmed": 0, "invalid_result": 0}
        self.listen_event(self.visual_review_result, "codexon_security_review_result")
        self.listen_event(self.visual_bridge_probe, "codexon_security_filter_probe")
        self.last_alert = {}
        self.frigate_events = set()
        self.cameras = {}
        self.frigate_camera_names = {}
        self.off_timers = {}
        self.presence_light_timers = {}
        self.presence_lights_active = set()
        self.presence_generation = 0
        self.disarm_latched = False
        self.alarm_nest_voice_until = 0.0
        self.alarm_mobile_voice_until = 0.0
        self.active_notification_tags = set()
        self.mobile_delivery = None
        self.mobile_delivery_timer = None
        self.listen_event(self.mobile_notification_received, "mobile_app_notification_received")
        self.listen_event(self.mobile_delivery_probe, "codexon_security_mobile_delivery_probe")
        self.delayed_timers = {}
        self.alternating_actions = {}
        self.confirmation_hits = {}
        self.alarm_generation = 0
        self.counter_history_restored = False
        self.listen_event(self.cancel_alarm_requested, "codexon_security_cancel_alarm")
        self.test_sensor = self.args.get(
            "test_sensor", "sensor.camera_security_test_count"
        )
        self.test_button = self.args.get("test_button")
        self.test_window_seconds = int(self.args.get("test_window_seconds", 180))
        self.test_aliases = self.args.get("test_aliases", {})
        self.test_hits = {}
        self.test_events = {}
        self.test_labels = {}
        self.test_started_at = None
        self.test_reset_timer = None
        self.simulacro_button = self.args.get("simulacro_button", "input_button.simulacro")
        self.simulacro_deadline = 0.0
        self.simulacro_started = 0.0
        self.simulacro_timer = None
        self.simulacro_hits = {"general": {}, "nocturna": {}}
        self.simulacro_last_message = ""
        self.simulacro_result = "listo"
        self.simulacro_notifications_sent = 0
        self.simulacro_notification_errors = 0
        self.simulacro_visual_results = {}
        self.listen_event(self.simulacro_visual_result, "codexon_security_simulacro_visual_result")

        for camera in self.args.get("cameras", []):
            name = camera["name"]
            self.cameras[name] = camera
            for alias in camera.get("frigate_names", [name]):
                if alias in self.frigate_camera_names:
                    raise ValueError(f"Duplicate Frigate camera mapping: {alias}")
                self.frigate_camera_names[alias] = name
            self._listen_camera_sensors(camera, "motion_sensors", "motion")
            self._listen_camera_sensors(camera, "person_sensors", "person")

        for presence_light in self.args.get("presence_lights", []):
            entity = presence_light["entity"]
            if self.entity_exists(entity):
                self.listen_state(
                    self.presence_light_changed,
                    entity,
                    new="on",
                    presence_light=presence_light,
                )
            else:
                self.log(f"Presence light sensor not found: {entity}", level="WARNING")

        for sensor in self.args.get("physical_sensors", []):
            entity = sensor["entity"]
            if self.entity_exists(entity):
                self.register_test_sensor(entity, sensor.get("message", entity))
                self.listen_state(
                    self.physical_sensor_changed,
                    entity,
                    new="on",
                    sensor=sensor,
                )
            else:
                self.log(f"Physical sensor not found: {entity}", level="WARNING")

        if self.frigate_event_sensor:
            self.listen_state(self.frigate_event_changed, self.frigate_event_sensor)

        self.listen_state(self.arm_state_changed, self.arm_switch)
        if self.night_arm_switch:
            self.listen_state(self.arm_state_changed, self.night_arm_switch)
        for entity in self.night_ack_entities:
            self.listen_state(self.night_ack_changed, entity, new="off")
        for entity in self.alarm_covers:
            self.listen_state(self.alarm_cover_changed, entity)
        if self.test_button:
            self.listen_state(self.test_button_pressed, self.test_button)
        self.listen_state(self.simulacro_pressed, self.simulacro_button)
        self.restore_simulacro()
        self.run_every(self.prune_test_hits, "now+10", 10)
        self.run_in(self.restore_test_history, 1)
        self.publish_test_count()
        self.publish_last_alert()
        self.publish_status()
        self.publish_visual_status()
        self.log(
            f"CameraSecurity ready: {len(self.cameras)} cameras, "
            f"armed={self.get_state(self.arm_switch)}"
        )

    def simulacro_active(self):
        return time.time() < self.simulacro_deadline

    def publish_simulacro(self):
        self.set_state("sensor.camera_security_simulacro",
            state="en_curso" if self.simulacro_active() else self.simulacro_result,
            replace=True, attributes={"friendly_name": "Simulacro de seguridad",
                "icon": "mdi:shield-check", "window_seconds": self.SIMULACRO_SECONDS,
                "sound_channel": "alarm_stream", "started_at": self.simulacro_started,
                "ends_at": self.simulacro_deadline,
                "notification_policy": "immediate_voice_visual_v1",
                "visual_results": list(self.simulacro_visual_results.values()),
                "repeat_policy": "once_per_sensor_or_camera_per_drill",
                "notifications_sent": self.simulacro_notifications_sent,
                "notification_errors": self.simulacro_notification_errors,
                "general": [{"key": k, "name": v} for k, v in sorted(self.simulacro_hits["general"].items())],
                "nocturna": [{"key": k, "name": v} for k, v in sorted(self.simulacro_hits["nocturna"].items())],
                "last_message": self.simulacro_last_message})

    def restore_simulacro(self):
        state = self.get_state("sensor.camera_security_simulacro", attribute="all") or {}
        attrs = state.get("attributes") or {}
        self.simulacro_last_message = str(attrs.get("last_message") or "")
        self.simulacro_notifications_sent = int(attrs.get("notifications_sent") or 0)
        self.simulacro_notification_errors = int(attrs.get("notification_errors") or 0)
        self.simulacro_visual_results = {str(r["case_id"])+":"+r["verdict"]: r for r in attrs.get("visual_results", []) if isinstance(r,dict) and type(r.get("case_id")) is int and r.get("verdict") in {"person_visible","supported_false_positive","inconclusive"}}
        if state.get("state") == "en_curso":
            try:
                deadline = float(attrs.get("ends_at", 0))
                started = float(attrs.get("started_at", 0))
                if not 0 < deadline - started <= self.SIMULACRO_SECONDS + 1 or deadline > time.time() + self.SIMULACRO_SECONDS + 1:
                    raise ValueError("Ventana inválida")
                self.simulacro_started, self.simulacro_deadline = started, deadline
                for mode in self.simulacro_hits:
                    self.simulacro_hits[mode] = {r["key"]: r["name"] for r in attrs.get(mode, []) if isinstance(r, dict) and isinstance(r.get("key"), str) and isinstance(r.get("name"), str)}
                if self.simulacro_active():
                    self.simulacro_timer = self.run_in(self.finish_simulacro, max(1, deadline - time.time()), started_at=started)
                else:
                    self.finish_simulacro({"started_at": started})
                    return
            except (TypeError, ValueError, KeyError):
                self.simulacro_deadline = 0.0
        elif self.simulacro_last_message:
            self.simulacro_result = state["state"] if state.get("state") in ("error_envio", "cancelado") else "finalizado"
        self.publish_simulacro()

    def simulacro_pressed(self, entity, attribute, old, new, kwargs):
        if self.simulacro_active():
            return  # Repeated presses never extend the drill or create another summary.
        if self.simulacro_timer is not None:
            self.cancel_timer(self.simulacro_timer)
        if self.simulacro_deadline:
            self.finish_simulacro({"started_at": self.simulacro_started})
        self.simulacro_started = time.time()
        self.simulacro_deadline = self.simulacro_started + self.SIMULACRO_SECONDS
        self.simulacro_hits = {"general": {}, "nocturna": {}}
        self.simulacro_notifications_sent = 0
        self.simulacro_notification_errors = 0
        self.simulacro_visual_results = {}
        self.confirmation_hits.clear()
        self.simulacro_last_message = ""
        self.simulacro_result = "listo"
        self.simulacro_timer = self.run_in(self.finish_simulacro, self.SIMULACRO_SECONDS, started_at=self.simulacro_started)
        for name, pending in list(self.visual_pending.items()):
            self.cancel_timer(pending["timer"])
            self.record_simulacro_camera(pending["camera"])
            self.visual_pending.pop(name, None)
        self.publish_visual_status()
        for camera in self.cameras.values():
            if any(self.get_state(e) == "on" for e in camera.get("person_sensors", [])):
                self.record_simulacro_camera(camera)
        for sensor in self.args.get("physical_sensors", []):
            if self.get_state(sensor["entity"]) == "on":
                self.record_simulacro_sensor(sensor["entity"])
        self.publish_simulacro()
        self.log(f"Simulacro started: immediate voice alerts, testing both alarm coverages for {self.SIMULACRO_SECONDS}s")

    def record_simulacro_camera(self, camera):
        if not self.simulacro_active():
            return
        key = "camera:" + camera["name"]
        if key in self.simulacro_hits["general"]:
            return
        name = "Cámara " + camera["name"].removeprefix("camara_").replace("_", " ").capitalize()
        self.simulacro_hits["general"][key] = name
        if any(e in self.night_sensors for e in camera.get("person_sensors", [])):
            self.simulacro_hits["nocturna"][key] = name
        self.announce_simulacro_detection(key, name)

    def record_simulacro_sensor(self, entity):
        if not self.simulacro_active():
            return
        key = "sensor:" + entity
        if key in self.simulacro_hits["general"]:
            return
        self.simulacro_hits["general"][key] = self.test_labels.get(entity, entity)
        if entity in self.night_sensors:
            self.simulacro_hits["nocturna"][key] = self.test_labels.get(entity, entity)
        self.announce_simulacro_detection(key, self.test_labels.get(entity, entity))

    def announce_simulacro_detection(self, key, name):
        coverage = "General y nocturna" if key in self.simulacro_hits["nocturna"] else "General"
        self.simulacro_last_message = (f"Simulacro. Detección en {name}. Verificando imagen. {coverage}."
            if key.startswith("camera:") else f"Simulacro. Sensor {name} activado. {coverage}.")
        self.publish_simulacro()  # Persist deduplication before sending the voice command.
        try:
            self.announce_mobile(self.simulacro_last_message, test=True)
        except Exception:
            self.simulacro_notification_errors += 1
            self.log("Simulacro voice notification could not be sent: " + name, level="WARNING")
        else:
            self.simulacro_notifications_sent += 1
        self.publish_simulacro()

    def simulacro_visual_result(self, event, data, kwargs):
        if not isinstance(data, dict) or self.simulacro_result == "cancelado":
            return
        try:
            if float(data.get("simulacro_started_at",0)) != self.simulacro_started:
                return
            if float(data.get("simulacro_ends_at",0)) != self.simulacro_started + self.SIMULACRO_SECONDS or time.time() > self.simulacro_started + self.SIMULACRO_SECONDS + 60:
                return
        except (TypeError, ValueError):
            return
        camera = self.cameras.get(data.get("camera"))
        case_id, result = data.get("case_id"), data.get("result")
        if camera is None or type(case_id) is not int or case_id <= 0 or not isinstance(result,dict):
            return
        verdict = result.get("visual_verdict")
        if verdict not in {"person_visible","supported_false_positive","inconclusive"}:
            return
        proof = data.get("proof") or {}
        if verdict == "person_visible" and not (type(result.get("person_count")) is int and result["person_count"] > 0 and result.get("image_quality") == "adequate" and result.get("observations")):
            verdict = "inconclusive"
        if verdict == "supported_false_positive" and not (result.get("person_count") == 0 and result.get("image_quality") == "adequate" and isinstance(proof,dict) and proof.get("matched") is True and proof.get("adequate") is True and proof.get("person_count") == 0 and type(proof.get("distinct_models")) is int and proof["distinct_models"] >= 2):
            verdict = "inconclusive"
        key = str(case_id)+":"+verdict
        if key in self.simulacro_visual_results:
            return
        if self.simulacro_active() and "camera:"+camera["name"] not in self.simulacro_hits["general"]:
            self.record_simulacro_camera(camera)
        zone = camera["name"].removeprefix("camara_").replace("_"," ")
        outcome = {"person_visible": "persona visible confirmada por la IA", "supported_false_positive": "falsa detección descartada", "inconclusive": "imagen sin verificar"}[verdict]
        message = f"Simulacro. Cámara {zone}. {outcome}."
        self.simulacro_visual_results[key] = {"case_id":case_id,"camera":camera["name"],"verdict":verdict,"message":message}
        self.publish_simulacro()
        try:
            self.announce_mobile(message, test=True)
        except Exception:
            self.simulacro_notification_errors += 1
            self.log("Simulacro visual result voice failed",level="WARNING")
        self.publish_simulacro()

    def cancel_simulacro(self):
        if not self.simulacro_started or time.time() > self.simulacro_started + self.SIMULACRO_SECONDS + 60:
            return
        if self.simulacro_timer is not None:
            self.cancel_timer(self.simulacro_timer)
        self.simulacro_timer = None
        self.simulacro_deadline = 0.0
        self.simulacro_result = "cancelado"
        self.publish_simulacro()

    def finish_simulacro(self, kwargs):
        if self.simulacro_deadline == 0.0 or kwargs.get("started_at") != self.simulacro_started:
            return
        self.simulacro_timer = None
        self.simulacro_deadline = 0.0
        self.confirmation_hits.clear()
        lines = [f"SIMULACRO · resumen de {self.SIMULACRO_SECONDS // 60} minutos"]
        for mode, label in [("general", "General"), ("nocturna", "Nocturna")]:
            names = sorted(set(self.simulacro_hits[mode].values()))
            lines.append(label + ": " + ("; ".join("presencia " + name for name in names) if names else "sin presencias detectadas"))
        self.simulacro_last_message = "\n".join(lines)
        # Keep the summary in HA; detections have already been spoken individually.
        self.simulacro_result = "error_envio" if self.simulacro_notification_errors else "finalizado"
        self.publish_simulacro()
        self.log("Simulacro finished: normal alarm handling resumed")

    def arm_state_changed(self, entity, attribute, old, new, kwargs):
        self.confirmation_hits.clear()
        if new == "off":
            self.disarm_latched = True
            self.cancel_current_alarm("night_disarmed" if entity == self.night_arm_switch else "general_disarmed", full_stop=True)
            for arm in (self.arm_switch, self.night_arm_switch):
                if arm and arm != entity and self.get_state(arm) == "on":
                    self.call_service(arm.split(".",1)[0]+"/turn_off",entity_id=arm)
        elif new == "on":
            self.disarm_latched = False
        self.publish_status()

    def cancel_alarm_requested(self, event, data, kwargs):
        self.cancel_current_alarm("manual")

    def cancel_current_alarm(self, reason, full_stop=False):
        self.alarm_generation += 1
        self.cancel_simulacro()
        self.confirmation_hits.clear()
        self.cancel_night_escalation()
        if self.alarm_cover_timer:
            self.cancel_timer(self.alarm_cover_timer)
            self.alarm_cover_timer = None
        for pending in list(self.visual_pending.values()):
            self.cancel_timer(pending["timer"])
        self.visual_pending.clear()
        self.publish_visual_status()
        self.cancel_all_deterrents()
        self.stop_alarm_covers(all_configured=full_stop)
        if full_stop:
            self.stop_presence_lights()
            self.stop_alarm_sirens()
        if full_stop or time.time() < self.alarm_mobile_voice_until:
            self.call_service(self.notify_service,message="command_stop_tts",data={"ttl":0,"priority":"high"})
        self.alarm_mobile_voice_until = 0.0
        for tag in self.active_notification_tags:
            self.call_service(self.notify_service,message="clear_notification",data={"tag":tag})
        self.active_notification_tags.clear()
        if self.mobile_delivery_timer:
            self.cancel_timer(self.mobile_delivery_timer)
            self.mobile_delivery_timer = None
        if self.mobile_delivery and self.mobile_delivery["status"] == "enviado":
            self.mobile_delivery["status"] = "cancelado"
            self.publish_mobile_delivery()
        if self.nest_player and time.time() < self.alarm_nest_voice_until:
            self.call_service("media_player/media_stop",entity_id=self.nest_player)
        self.alarm_nest_voice_until = 0.0
        self.set_state("sensor.camera_security_cancel_alarm", state=self.datetime().isoformat(timespec="seconds"),
            attributes={"friendly_name":"Última cancelación de alarma", "reason":reason,
                "general_armed":self.get_state(self.arm_switch)=="on",
                "night_armed":self.get_state(self.night_arm_switch)=="on"})
        self.log("Current alarm cancelled: " + reason)

    def stop_presence_lights(self):
        self.presence_generation += 1
        for handle in self.presence_light_timers.values():
            self.cancel_timer(handle)
        self.presence_light_timers.clear()
        for entity in list(self.presence_lights_active):
            self.turn_off(entity)
        self.presence_lights_active.clear()

    def stop_alarm_sirens(self):
        for entity, state in (self.get_state() or {}).items():
            value = state.get("state") if isinstance(state, dict) else state
            if entity.startswith("siren.") and value == "on":
                self.call_service("siren/turn_off",entity_id=entity)
        if self.get_state("switch.automatismosf2_sirena") == "on":
            self.call_service("switch/turn_off",entity_id="switch.automatismosf2_sirena")

    def stop_alarm_covers(self, all_configured=False):
        entities = set(self.alarm_covers) if all_configured else set(self.active_alarm_covers)
        for entity in entities:
            if self.get_state(entity) not in ("unavailable", "unknown", "closed"):
                self.call_service("cover/stop_cover",entity_id=entity)
        self.active_alarm_covers.clear()

    def alarm_cover_changed(self, entity, attribute, old, new, kwargs):
        if new in ("closed", "opening"):
            self.active_alarm_covers.discard(entity)

    def cancel_all_deterrents(self):
        for entity, timer in list(self.off_timers.items()):
            self.cancel_timer(timer)
            domain = entity.split(".", 1)[0]
            if entity not in self.presence_lights_active:
                self.call_service(f"{domain}/turn_off", entity_id=entity)
            self.log(f"Deterrent disabled: {entity}")
        self.off_timers.clear()
        for key, timer in list(self.delayed_timers.items()):
            self.cancel_timer(timer)
        self.delayed_timers.clear()
        for key in list(self.alternating_actions):
            self.stop_alternating_deterrent({"key":key})

    def night_ack_changed(self, entity, attribute, old, new, kwargs):
        self.cancel_night_escalation()

    def publish_status(self, last_alert=None):
        attributes = {
            "friendly_name": "Camera security status",
            "armed": self.get_state(self.arm_switch) == "on",
            "night_armed": self.night_arm_switch
            and self.get_state(self.night_arm_switch) == "on",
            "cameras": sorted(self.cameras),
            "camera_count": len(self.cameras),
            "motion_alerts_enabled": self.motion_alerts_enabled,
            "camera_trigger_policy": "person_and_motion" if self.motion_alerts_enabled else "person_only",
            "person_cooldown_seconds": self.person_cooldown,
            "visual_filter_enabled": self.visual_filter_enabled,
            "visual_filter_wait_seconds": self.visual_filter_wait,
            "cancel_alarm_button": "script.cancelar_alarma_actual",
            "counter_history_restored": self.counter_history_restored,
            "disarm_policy": "full_stop_both_modes",
            "mobile_alert_policy": "spoken_alerts_like_doorbell_v1",
            "frigate_event_sensor": self.frigate_event_sensor,
        }
        if last_alert:
            attributes["last_alert"] = last_alert
        self.set_state(
            "sensor.camera_security_status",
            state="armed"
            if attributes["armed"]
            else "night"
            if attributes["night_armed"]
            else "disarmed",
            attributes=attributes,
        )

    def publish_last_alert(self, state="unknown", **attributes):
        self.set_state(
            "sensor.camera_security_last_alert",
            state=state,
            attributes={
                "friendly_name": "Camera security last alert",
                **attributes,
            },
        )

    def _listen_camera_sensors(self, camera, key, kind):
        for entity in camera.get(key, []):
            if self.entity_exists(entity):
                self.register_test_sensor(entity, f"{camera['name']} {kind}")
                self.listen_state(
                    self.camera_sensor_changed,
                    entity,
                    new="on",
                    camera=camera,
                    kind=kind,
                )
            else:
                self.log(f"Camera sensor not found: {entity}", level="WARNING")

    def register_test_sensor(self, entity, label):
        if entity not in self.test_labels:
            self.listen_state(self.security_test_sensor_changed, entity, new="on")
        self.test_labels[entity] = self.test_aliases.get(entity, label)

    def security_test_sensor_changed(self, entity, attribute, old, new, kwargs):
        seen_at = self.datetime()
        self.test_hits[entity] = seen_at
        self.test_events.setdefault(entity, []).append(seen_at)
        self.publish_test_count()

    def restore_test_history(self, kwargs=None):
        """Keep rolling counters across this app's code reloads."""
        try:
            now = self.datetime()
            aware = self.datetime(aware=True)
            histories = self.get_history(entity_id=sorted(self.test_labels),
                start_time=aware-timedelta(hours=24), no_attributes=True, significant_changes_only=True)
            if not isinstance(histories, list):
                raise ValueError("History missing")
            for history in histories:
                if not history:
                    continue
                entity = history[0].get("entity_id")
                if entity not in self.test_labels:
                    continue
                previous = history[0].get("state")
                events = set(self.test_events.get(entity, []))
                for state in history[1:]:
                    if state.get("state") == "on" and previous != "on":
                        value = state["last_changed"]
                        seen = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
                        seen = seen.astimezone(aware.tzinfo)
                        if now.tzinfo is None:
                            seen = seen.replace(tzinfo=None)
                        if now-timedelta(hours=24) <= seen <= now:
                            events.add(seen)
                    previous = state.get("state")
                self.test_events[entity] = sorted(events)
            self.counter_history_restored = True
            self.publish_test_count()
            self.publish_status()
        except Exception as exc:
            self.log("Counter history restore postponed: " + type(exc).__name__, level="WARNING")
            self.run_in(self.restore_test_history, 30)

    def test_button_pressed(self, entity, attribute, old, new, kwargs):
        self.test_started_at = self.datetime()
        if self.test_reset_timer:
            self.cancel_timer(self.test_reset_timer)
        self.test_reset_timer = self.run_in(
            self.finish_test_window, self.test_window_seconds
        )
        self.publish_test_count()
        self.log(f"Security sensor test started for {self.test_window_seconds}s")

    def finish_test_window(self, kwargs):
        self.test_reset_timer = None
        self.test_started_at = None
        self.test_hits = {}
        self.publish_test_count()
        self.log("Security sensor test window finished")

    def prune_test_hits(self, kwargs=None):
        cutoff = self.datetime() - timedelta(seconds=self.test_window_seconds)
        self.test_hits = {
            entity: seen_at
            for entity, seen_at in self.test_hits.items()
            if seen_at >= cutoff
        }
        event_cutoff = self.datetime() - timedelta(hours=24)
        self.test_events = {
            entity: [seen_at for seen_at in events if seen_at >= event_cutoff]
            for entity, events in self.test_events.items()
        }
        self.publish_test_count()

    def publish_test_count(self):
        now = self.datetime()
        cutoff = now - timedelta(seconds=self.test_window_seconds)
        active = {
            entity: seen_at
            for entity, seen_at in self.test_hits.items()
            if seen_at >= cutoff
        }
        self.test_hits = active
        active_entities = sorted(active, key=lambda item: self.test_labels.get(item, item))
        window_cutoffs = {
            "5min": now - timedelta(minutes=5),
            "1h": now - timedelta(hours=1),
            "24h": now - timedelta(hours=24),
        }
        sensor_rows = []
        for entity in sorted(self.test_labels):
            events = self.test_events.get(entity, [])
            sensor_rows.append(
                {
                    "entity": entity,
                    "name": self.test_labels.get(entity, entity),
                    "count_5min": sum(1 for seen_at in events if seen_at >= window_cutoffs["5min"]),
                    "count_1h": sum(1 for seen_at in events if seen_at >= window_cutoffs["1h"]),
                    "count_24h": sum(1 for seen_at in events if seen_at >= window_cutoffs["24h"]),
                    "active_test": entity in active,
                }
            )
        total_5min = sum(row["count_5min"] for row in sensor_rows)
        total_1h = sum(row["count_1h"] for row in sensor_rows)
        total_24h = sum(row["count_24h"] for row in sensor_rows)
        sensor_table = "\n".join(
            [
                "| nombre sensor | 5min | 1h | 24h |",
                "|---|---:|---:|---:|",
                *[
                    f"| {row['name']} | {row['count_5min']} | {row['count_1h']} | {row['count_24h']} |"
                    for row in sensor_rows
                ],
            ]
        )
        self.set_state(
            self.test_sensor,
            state=str(total_5min),
            replace=True,
            attributes={
                "friendly_name": "Activaciones recientes de sensores de seguridad, alerta y presencia",
                "icon": "mdi:shield-check",
                "window_seconds": self.test_window_seconds,
                "total_5min": total_5min,
                "total_1h": total_1h,
                "total_24h": total_24h,
                "sensor_rows": sensor_rows,
                "sensor_table": sensor_table,
                "test_active": self.test_started_at is not None,
                "test_started_at": self.test_started_at.isoformat(timespec="seconds")
                if self.test_started_at
                else None,
                "available_sensors": sorted(self.test_labels),
                "available_sensor_names": [
                    self.test_labels[entity] for entity in sorted(self.test_labels)
                ],
                "available_count": len(self.test_labels),
                "active_sensors": active_entities,
                "active_sensor_names": [
                    self.test_labels.get(entity, entity) for entity in active_entities
                ],
            },
        )

    def camera_sensor_changed(self, entity, attribute, old, new, kwargs):
        camera = kwargs["camera"]
        kind = kwargs["kind"]
        if kind == "motion" and not self.motion_alerts_enabled:
            return
        if self.simulacro_active():
            if kind == "person":
                self.record_simulacro_camera(camera)
            return

        night_armed = (
            self.night_arm_switch
            and self.get_state(self.night_arm_switch) == "on"
            and entity in self.night_sensors
        )

        handled = self.handle_alert(
            camera=camera,
            kind=kind,
            source=entity,
            message=self.camera_person_message(camera) if kind == "person" else f"{kind.title()} detected by {camera['name']}",
            critical=kind == "person",
            allow_disarmed=night_armed,
        )
        if handled and night_armed:
            self.run_deterrents(self.night_deterrents)

    def presence_light_changed(self, entity, attribute, old, new, kwargs):
        if self.simulacro_active() or self.disarm_latched or not self.security_armed():
            return
        if not self.now_is_between("sunset", "sunrise"):
            return

        presence_light = kwargs["presence_light"]
        duration = int(presence_light.get("duration", 300))
        for light in presence_light["lights"]:
            if not self.entity_exists(light):
                self.log(f"Presence light not found: {light}", level="WARNING")
                continue
            if self.get_state(light) != "on":
                self.turn_on(light)
                self.presence_lights_active.add(light)
            if light not in self.presence_lights_active:
                continue
            old_timer = self.presence_light_timers.pop(light, None)
            if old_timer:
                self.cancel_timer(old_timer)
            self.presence_light_timers[light] = self.run_in(
                self.stop_presence_light,
                duration,
                light=light,
                generation=self.presence_generation,
            )
        self.log(f"Night presence lights enabled by {entity} for {duration}s")

    def stop_presence_light(self, kwargs):
        if kwargs.get("generation",self.presence_generation) != self.presence_generation:
            return
        light = kwargs["light"]
        self.presence_light_timers.pop(light, None)
        self.presence_lights_active.discard(light)
        self.turn_off(light)
        self.log(f"Night presence light disabled: {light}")

    def physical_sensor_changed(self, entity, attribute, old, new, kwargs):
        if self.simulacro_active():
            self.record_simulacro_sensor(entity)
            return
        if self.disarm_latched:
            return
        sensor = kwargs["sensor"]
        general_armed = self.get_state(self.arm_switch) == "on"
        night_armed = (
            self.night_arm_switch
            and self.get_state(self.night_arm_switch) == "on"
            and entity in self.night_sensors
        )
        if not general_armed and not night_armed:
            self.log(f"Ignoring while disarmed: {entity}")
            return

        enabled_switch = sensor.get("enabled_switch")
        if not night_armed and enabled_switch and self.get_state(enabled_switch) != "on":
            self.confirmation_hits.pop(entity, None)
            self.log(f"Ignoring disabled zone: {entity}")
            return

        if not self.physical_sensor_confirmed(entity, sensor):
            return

        camera = self.cameras.get(sensor.get("camera"))
        handled = self.handle_alert(
            camera=camera,
            kind="physical",
            source=entity,
            message=sensor.get("message", f"Physical alarm: {entity}"),
            critical=True,
            announce=True,
            allow_disarmed=night_armed,
        )
        self.schedule_alarm_covers(entity)
        if handled:
            if night_armed:
                self.run_deterrents(self.night_deterrents)
                self.schedule_night_escalation(sensor)
            else:
                self.run_deterrents(sensor.get("deterrents", []))
                self.run_alternating_deterrents(sensor.get("alternating_deterrents", []))

    def physical_sensor_confirmed(self, entity, sensor):
        required = max(1, int(sensor.get("confirmations_required", 1)))
        if required == 1:
            return True

        window = max(1, int(sensor.get("confirmation_window_seconds", 5)))
        now = self.datetime()
        cutoff = now - timedelta(seconds=window)
        hits = [
            seen_at
            for seen_at in self.confirmation_hits.get(entity, [])
            if seen_at >= cutoff
        ]
        hits.append(now)
        if len(hits) < required:
            self.confirmation_hits[entity] = hits
            self.log(
                f"Waiting for confirmation: {entity} "
                f"{len(hits)}/{required} activations in {window}s"
            )
            return False

        self.confirmation_hits.pop(entity, None)
        self.log(
            f"Physical sensor confirmed: {entity} "
            f"{required} activations in {window}s"
        )
        return True

    def schedule_alarm_covers(self, source):
        if source not in self.alarm_cover_triggers or not self.alarm_covers:
            return
        if self.alarm_cover_timer:
            self.cancel_timer(self.alarm_cover_timer)
        self.alarm_cover_timer = self.run_in(
            self.close_alarm_covers,
            self.alarm_cover_delay,
            source=source,
            generation=self.alarm_generation,
        )
        self.log(
            f"Alarm cover close scheduled in {self.alarm_cover_delay}s by {source}"
        )

    def close_alarm_covers(self, kwargs):
        if kwargs.get("generation", self.alarm_generation) != self.alarm_generation:
            return
        self.alarm_cover_timer = None
        if self.simulacro_active():
            return
        general_armed = self.get_state(self.arm_switch) == "on"
        night_armed = (
            self.night_arm_switch
            and self.get_state(self.night_arm_switch) == "on"
        )
        if not general_armed and not night_armed:
            self.log("Alarm cover close skipped: alarm is no longer armed")
            return
        self.call_service("cover/close_cover", entity_id=self.alarm_covers)
        self.active_alarm_covers.update(entity for entity in self.alarm_covers if self.get_state(entity)!="closed")
        self.log(
            f"Alarm covers close requested by {kwargs['source']}: "
            f"{', '.join(self.alarm_covers)}"
        )

    def schedule_night_escalation(self, sensor):
        self.cancel_night_escalation()
        self.night_escalation_timer = self.run_in(
            self.escalate_night_alarm,
            60,
            source=sensor["entity"],
            message=sensor.get("message", f"Physical alarm: {sensor['entity']}"),
            generation=self.alarm_generation,
        )
        self.log(f"Night escalation armed for {sensor['entity']} in 60s")

    def cancel_night_escalation(self):
        if self.night_escalation_timer:
            self.cancel_timer(self.night_escalation_timer)
            self.night_escalation_timer = None
            self.log("Night escalation cancelled")

    def escalate_night_alarm(self, kwargs):
        if kwargs.get("generation", self.alarm_generation) != self.alarm_generation:
            return
        self.night_escalation_timer = None
        if self.simulacro_active():
            return
        if self.night_arm_switch and self.get_state(self.night_arm_switch) != "on":
            self.log("Night escalation skipped: night mode disabled")
            return
        if any(self.get_state(entity) != "on" for entity in self.night_ack_entities):
            self.log("Night escalation skipped: acknowledged by NSPanel relay off")
            return

        message = f"Escalada seguridad noche: {kwargs['message']}"
        self.announce_mobile(message)
        self.announce_nest(message)
        self.run_deterrents(self.night_escalation_deterrents)
        self.publish_status(last_alert=self.datetime().isoformat(timespec="seconds"))
        self.log(f"Night escalation fired: {kwargs['source']}")

    def frigate_event_changed(self, entity, attribute, old, new, kwargs):
        state = self.get_state(entity, attribute="all") or {}
        attrs = state.get("attributes", {})
        after = attrs.get("after") or {}
        event_id = after.get("id")
        label = after.get("label")
        camera_name = after.get("camera")

        if not event_id or not label or not camera_name:
            return
        if event_id in self.frigate_events:
            return
        if label not in self.args.get("frigate_labels", ["person"]):
            return

        name = self.frigate_camera_names.get(camera_name)
        camera = self.cameras.get(name)
        if camera is None:
            self.log(f"Ignoring unmapped Frigate camera: {camera_name}", level="WARNING")
            return

        if self.simulacro_active():
            self.frigate_events.add(event_id)
            if label == "person" and (not isinstance(after.get("end_time"), (int, float)) or after["end_time"] >= self.simulacro_started):
                self.record_simulacro_camera(camera)
            return
        if self.disarm_latched:
            return
        self.frigate_events.add(event_id)
        if len(self.frigate_events) > 500:
            self.frigate_events.clear()
            self.frigate_events.add(event_id)

        night_armed = (
            label == "person"
            and self.night_arm_switch
            and self.get_state(self.night_arm_switch) == "on"
            and any(sensor in self.night_sensors for sensor in camera.get("person_sensors", []))
        )
        if label == "person" and self.visual_filter_enabled:
            self.defer_visual_alert(camera, after, night_armed)
            return
        handled = self.handle_alert(
            camera=camera,
            kind=f"frigate_{label}",
            source=f"frigate:{camera_name}:{event_id}",
            message=self.camera_person_message(camera) if label == "person" else f"Frigate detected {label} in {camera['name']}",
            critical=label == "person",
            allow_disarmed=night_armed,
        )
        if handled and night_armed:
            self.run_deterrents(self.night_deterrents)

    def visual_bridge_probe(self, event_name, data, kwargs):
        nonce = data.get("nonce") if isinstance(data, dict) else None
        if isinstance(nonce, str) and len(nonce) == 32 and all(c in "0123456789abcdef" for c in nonce):
            self.fire_event("codexon_security_filter_probe_result", nonce=nonce, enabled=self.visual_filter_enabled, timeout_seconds=self.visual_filter_wait)

    def publish_visual_status(self):
        self.set_state("sensor.camera_security_visual_filter", state="active" if self.visual_filter_enabled else "disabled", attributes={"friendly_name": "Filtro visual de avisos de cámara", "timeout_seconds": self.visual_filter_wait, "pending_cameras": sorted(self.visual_pending), "outcomes": self.visual_counts, "scope": "frigate_person_event"})

    def defer_visual_alert(self, camera, after, night_armed):
        if self.get_state(self.arm_switch) != "on" and not night_armed:
            return
        name = camera["name"]
        last = self.last_alert.get("person:" + name)
        if last and (datetime.now() - last).total_seconds() < self.person_cooldown:
            return
        if name in self.visual_pending:
            # A second track can be a different person: do not apply the first verdict to it.
            if self.visual_pending[name]["event_id"] != after["id"]:
                self.resolve_visual_alert(name, "notify", "invalid_result")
            return
        request_id = uuid.uuid4().hex
        now = datetime.now().astimezone()
        pending = {"request_id": request_id, "event_id": after["id"], "camera": camera, "source": f"frigate:{after['camera']}:{after['id']}", "message": self.camera_person_message(camera), "deadline": time.time() + self.visual_filter_wait}
        pending["timer"] = self.run_in(self.visual_review_timeout, self.visual_filter_wait, camera=name, request_id=request_id)
        self.visual_pending[name] = pending
        self.publish_visual_status()
        try:
            self.fire_event("codexon_security_review_requested", request_id=request_id, camera=name, requested_at=now.isoformat(), deadline=pending["deadline"], after=after)
        except Exception:
            self.resolve_visual_alert(name, "notify", "invalid_result")

    def visual_review_result(self, event_name, data, kwargs):
        if not isinstance(data, dict):
            return
        name = data.get("camera")
        pending = self.visual_pending.get(name)
        if not pending or data.get("request_id") != pending["request_id"] or data.get("event_id") != pending["event_id"]:
            return
        if time.time() >= pending["deadline"]:
            self.resolve_visual_alert(name, "notify", "timeout")
            return
        proof = data.get("proof") or {}
        valid = isinstance(proof, dict) and proof.get("matched") is True and proof.get("adequate") is True and type(proof.get("person_count")) is int and proof["person_count"] == 0 and type(proof.get("distinct_models")) is int and proof["distinct_models"] >= 2 and type(data.get("case_id")) is int and data["case_id"] > 0
        if data.get("decision") == "suppress_false_positive" and valid:
            self.resolve_visual_alert(name, "suppress", "suppressed")
        else:
            self.resolve_visual_alert(name, "notify", "notified" if data.get("decision") == "notify" else "invalid_result")

    def visual_review_timeout(self, kwargs):
        pending = self.visual_pending.get(kwargs["camera"])
        if pending and pending["request_id"] == kwargs["request_id"]:
            self.resolve_visual_alert(kwargs["camera"], "notify", "timeout")

    def resolve_visual_alert(self, name, decision, outcome):
        pending = self.visual_pending.pop(name, None)
        if not pending:
            return
        self.cancel_timer(pending["timer"])
        last = self.last_alert.get("person:" + name)
        if last and (datetime.now() - last).total_seconds() < self.person_cooldown:
            outcome = "already_notified"
        else:
            camera = pending["camera"]
            night_armed = self.night_arm_switch and self.get_state(self.night_arm_switch) == "on" and any(sensor in self.night_sensors for sensor in camera.get("person_sensors", []))
            if self.get_state(self.arm_switch) != "on" and not night_armed:
                outcome = "disarmed"
            elif decision == "notify":
                handled = self.handle_alert(camera=camera, kind="frigate_person", source=pending["source"], message=pending["message"], critical=True, allow_disarmed=night_armed)
                if handled and night_armed:
                    self.run_deterrents(self.night_deterrents)
            else:
                self.log(f"Visual false positive filtered: {name}, event={pending['event_id']}")
        self.visual_counts[outcome] += 1
        self.publish_visual_status()
        try:
            self.fire_event("codexon_security_filter_outcome", request_id=pending["request_id"], camera=name, outcome=outcome)
        except Exception:
            self.log("Visual filter outcome could not be published", level="WARNING")

    def terminate(self):
        # A code reload must not silently lose an armed pending camera notification.
        for name in list(getattr(self, "visual_pending", {})):
            self.resolve_visual_alert(name, "notify", "timeout")

    def handle_alert(
        self,
        camera,
        kind,
        source,
        message,
        critical=False,
        image=None,
        click_action=None,
        announce=False,
        allow_disarmed=False,
    ):
        if self.simulacro_active() or self.disarm_latched:
            return False
        if self.get_state(self.arm_switch) != "on" and not allow_disarmed:
            self.log(f"Ignoring while disarmed: {source}")
            return False

        now = datetime.now()
        person_camera = camera and kind in {"person", "frigate_person"}
        alert_key = f"person:{camera['name']}" if person_camera else source
        cooldown = self.person_cooldown if person_camera else self.cooldown
        last = self.last_alert.get(alert_key)
        if last and (now - last).total_seconds() < cooldown:
            return False
        self.last_alert[alert_key] = now

        evidence = None
        use_event_image = camera and kind in {"person", "frigate_person", "physical"}
        if use_event_image and not image:
            try:
                evidence = event_image(camera, source,
                    Path(self.args.get("home_assistant_config_dir", Path(__file__).resolve().parents[2])),
                    base_url=self.args.get("frigate_url", "http://frigate:5000"))
            except Exception as error:
                self.log("Event attachment unavailable: %s", type(error).__name__, level="WARNING")
        snapshot = evidence["snapshot"] if evidence else self.take_snapshot(camera, now)
        camera_entity = camera.get("camera_entity") if camera else None
        image = image or (evidence["image"] if evidence else None)
        if not use_event_image:
            image = image or self.camera_proxy_url(camera_entity)
        click_action = click_action or image

        data = {
            "tag": f"camera_security_{alert_key}" if person_camera else f"camera_security_{kind}_{source}",
            "ttl": 0,
            "priority": "high" if critical else "normal",
        }
        if image:
            data["image"] = image
        if click_action:
            data["clickAction"] = click_action
        if critical:
            data.update(
                {
                    "channel": "alarm_stream",
                    "importance": "high",
                    "persistent": True,
                    "sound": "default",
                }
            )
        self.send_alarm_notification(message, data)
        if critical or announce:
            self.announce_mobile(message)
        if announce:
            self.announce_nest(message)
        self.publish_last_alert(
            state=now.isoformat(timespec="seconds"),
            source=source,
            kind=kind,
            camera=camera_entity,
            snapshot=snapshot,
            image_event_id=evidence["event_id"] if evidence else None,
            notification_image=image,
        )
        self.publish_status(last_alert=now.isoformat(timespec="seconds"))
        self.log(f"Alert submitted to mobile service: {message}; snapshot={snapshot}")
        return True

    def send_alarm_notification(self, message, data, title=None):
        if self.mobile_delivery_timer:
            self.cancel_timer(self.mobile_delivery_timer)
            self.mobile_delivery_timer = None
        delivery_id = uuid.uuid4().hex
        sent_epoch = int(time.time())
        self.mobile_delivery = {"id": delivery_id, "tag": data["tag"],
            "message": message, "status": "enviado",
            "delivery_kind": "voice" if message == "TTS" else "notification",
            "spoken_text": data.get("tts_text", ""),
            "receipt_timestamp": sent_epoch,
            "sent_at": self.datetime().isoformat(timespec="seconds")}
        self.active_notification_tags.add(data["tag"])
        # Use documented notification fields: the mobile gateway drops custom keys.
        data = {**data, "confirmation": True, "when": sent_epoch}
        night_only = self.get_state(self.arm_switch) != "on" and self.night_arm_switch and self.get_state(self.night_arm_switch) == "on"
        try:
            self.call_service(self.notify_service,
                title=title or ("Alerta de seguridad nocturna" if night_only else "Alerta de seguridad"),
                message=message, data=data)
        except Exception:
            self.mobile_delivery["status"] = "error_envio"
            self.publish_mobile_delivery()
            raise
        self.publish_mobile_delivery()
        self.mobile_delivery_timer = self.run_in(self.mobile_delivery_timeout, 30,
            delivery_id=delivery_id, generation=self.alarm_generation)

    def mobile_delivery_probe(self, event, data, kwargs):
        nonce = data.get("nonce") if isinstance(data, dict) else None
        if not isinstance(nonce, str) or len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
            return
        # Tests only the phone path; it never creates an intrusion or activates outputs.
        self.send_alarm_notification("PRUEBA: verificación del aviso de seguridad. No es una intrusión.",
            {"tag": "camera_security_delivery_probe", "ttl": 0, "priority": "high",
                "channel": "alarm_stream", "importance": "high", "persistent": True,
                "sound": "default", "clickAction": "/lovelace/seguridad"},
            title="Prueba de aviso de seguridad")
        self.announce_mobile("Prueba de seguridad. Presencia de persona en casa.", test=True)

    def mobile_notification_received(self, event, data, kwargs):
        delivery = self.mobile_delivery
        if not isinstance(data, dict) or not delivery or delivery["status"] not in ("enviado", "sin_confirmar"):
            return
        if data.get("tag") != delivery["tag"] or data.get("message") != delivery["message"] or str(data.get("when")) != str(delivery["receipt_timestamp"]):
            return
        if delivery.get("delivery_kind") == "voice" and data.get("tts_text") != delivery["spoken_text"]:
            return
        if self.mobile_delivery_timer:
            self.cancel_timer(self.mobile_delivery_timer)
            self.mobile_delivery_timer = None
        delivery["status"] = "recibido"
        delivery["received_at"] = self.datetime().isoformat(timespec="seconds")
        self.publish_mobile_delivery()
        self.log("Mobile confirmed receipt of security alert: " + delivery["tag"])

    def mobile_delivery_timeout(self, kwargs):
        delivery = self.mobile_delivery
        if kwargs["generation"] != self.alarm_generation or not delivery or kwargs["delivery_id"] != delivery["id"] or delivery["status"] != "enviado":
            return
        self.mobile_delivery_timer = None
        delivery["status"] = "sin_confirmar"
        self.publish_mobile_delivery()
        self.log("Mobile did not confirm security alert within 30s: " + delivery["tag"], level="WARNING")

    def publish_mobile_delivery(self):
        delivery = self.mobile_delivery
        if delivery:
            self.set_state("sensor.camera_security_mobile_delivery", state=delivery["status"], replace=True,
                attributes={"friendly_name": "Recepción del aviso de seguridad en el móvil", "icon": "mdi:cellphone-message",
                    "receipt_is_read_confirmation": False, "timeout_seconds": 30, **delivery})

    @staticmethod
    def camera_person_message(camera):
        zone = {"camara_casa": "casa", "camara_almacen": "almacén",
            "camara_oficina": "oficina", "cancela": "cancela",
            "garrofera": "garrofera", "olivera": "olivo"}.get(camera["name"], camera["name"].replace("_", " "))
        return "Alerta de seguridad. Presencia de persona en " + zone + "."

    def announce_mobile(self, message, test=False):
        if not test and (self.disarm_latched or not self.security_armed()):
            return
        self.alarm_mobile_voice_until = time.time()+120
        self.call_service(
            self.notify_service,
            message="command_volume_level",
            data={
                "media_stream": "alarm_stream",
                "command": 100,
            },
        )
        self.send_alarm_notification(
            "TTS",
            {
                "ttl": 0,
                "priority": "high",
                "media_stream": "alarm_stream",
                "tts_text": message,
                "tag": "camera_security_voice",
            },
        )

    def announce_nest(self, message):
        if not self.nest_player:
            return
        self.alarm_nest_voice_until = time.time()+120
        self.call_service(
            "tts/google_translate_say",
            entity_id=self.nest_player,
            message=message,
            language="es",
        )

    def run_deterrents(self, deterrents):
        for action in deterrents:
            delay = int(action.get("delay", 0))
            if delay:
                key = self.deterrent_key(action)
                old_timer = self.delayed_timers.pop(key, None)
                if old_timer:
                    self.cancel_timer(old_timer)
                self.delayed_timers[key] = self.run_in(
                    self.start_deterrent,
                    delay,
                    action=action,
                    key=key,
                    generation=self.alarm_generation,
                )
            else:
                self.activate_deterrent(action)

    def start_deterrent(self, kwargs):
        if kwargs.get("generation", self.alarm_generation) != self.alarm_generation:
            return
        self.delayed_timers.pop(kwargs["key"], None)
        self.activate_deterrent(kwargs["action"])

    def activate_deterrent(self, action):
        if self.simulacro_active() or self.disarm_latched or not self.security_armed():
            return
        entity = action["entity"]
        duration = int(action["duration"])
        data = {"entity_id": entity, **action.get("turn_on_data", {})}
        domain = entity.split(".", 1)[0]
        self.call_service(f"{domain}/turn_on", **data)

        old_timer = self.off_timers.pop(entity, None)
        if old_timer:
            self.cancel_timer(old_timer)
        self.off_timers[entity] = self.run_in(
            self.stop_deterrent,
            duration,
            entity=entity,
            generation=self.alarm_generation,
        )
        self.log(f"Deterrent enabled: {entity} for {duration}s")

    def security_armed(self):
        return self.get_state(self.arm_switch)=="on" or bool(self.night_arm_switch and self.get_state(self.night_arm_switch)=="on")

    def stop_deterrent(self, kwargs):
        if kwargs.get("generation", self.alarm_generation) != self.alarm_generation:
            return
        entity = kwargs["entity"]
        self.off_timers.pop(entity, None)
        if entity in self.presence_lights_active:
            self.log(f"Deterrent timeout skipped; presence light remains on: {entity}")
            return
        domain = entity.split(".", 1)[0]
        self.call_service(f"{domain}/turn_off", entity_id=entity)
        self.log(f"Deterrent disabled: {entity}")

    def run_alternating_deterrents(self, deterrents):
        for action in deterrents:
            entities = tuple(action["entities"])
            key = "|".join(entities)
            self.stop_alternating_deterrent({"key": key})

            self.alternating_actions[key] = {
                "entities": entities,
                "interval": int(action.get("interval", 2)),
                "index": 0,
            }
            self.set_alternating_entity(entities, 0)
            self.schedule_alternating_tick(key)
            self.alternating_actions[key]["stop_timer"] = self.run_in(
                self.stop_alternating_deterrent,
                int(action["duration"]),
                key=key,
            )
            self.log(
                f"Alternating deterrent enabled: {key} "
                f"for {action['duration']}s"
            )

    def schedule_alternating_tick(self, key):
        action = self.alternating_actions.get(key)
        if not action:
            return
        action["tick_timer"] = self.run_in(
            self.advance_alternating_deterrent,
            action["interval"],
            key=key,
        )

    def advance_alternating_deterrent(self, kwargs):
        key = kwargs["key"]
        action = self.alternating_actions.get(key)
        if not action:
            return
        action["index"] = (action["index"] + 1) % len(action["entities"])
        self.set_alternating_entity(action["entities"], action["index"])
        self.schedule_alternating_tick(key)

    def stop_alternating_deterrent(self, kwargs):
        key = kwargs["key"]
        action = self.alternating_actions.pop(key, None)
        if not action:
            return
        for timer_name in ("tick_timer", "stop_timer"):
            timer = action.get(timer_name)
            if timer:
                self.cancel_timer(timer)
        self.set_alternating_entity(action["entities"])
        self.log(f"Alternating deterrent disabled: {key}")

    def set_alternating_entity(self, entities, enabled_index=None):
        for index, entity in enumerate(entities):
            domain = entity.split(".", 1)[0]
            service = "turn_on" if index == enabled_index else "turn_off"
            self.call_service(f"{domain}/{service}", entity_id=entity)

    @staticmethod
    def deterrent_key(action):
        return action["entity"]

    def take_snapshot(self, camera, now):
        if not camera:
            return None
        camera_entity = camera.get("camera_entity")
        if not camera_entity or not self.entity_exists(camera_entity):
            self.log(f"Camera not found: {camera_entity}", level="WARNING")
            return None

        safe_name = camera["name"].replace("/", "_")
        filename = os.path.join(
            self.snapshot_dir,
            f"security_{safe_name}_{now.strftime('%Y%m%d_%H%M%S')}.jpg",
        )
        self.call_service(
            "camera/snapshot",
            entity_id=camera_entity,
            filename=filename,
        )
        return filename

    @staticmethod
    def camera_proxy_url(camera_entity):
        if not camera_entity:
            return None
        return f"/api/camera_proxy/{camera_entity}"
