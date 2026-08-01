# Automation plans

Codexon represents deterministic Home Assistant jobs with one versioned format:

```text
AUTOMATION_PLAN_V1 {json}
```

The prefix is only the persistent envelope used by the current SQLite task schema. The JSON is validated by
`automation/schema.py` and executed by `automation/engine.py`. New entities do not require new executors.

## Model

An automation plan contains:

- `conditions`: optional state or attribute comparisons.
- `condition_policy`: fail, complete, or reschedule when a condition is false.
- `steps`: Home Assistant service calls and short delays.

Supported condition operators are `eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, and `not_in`. They work with
`sensor`, `binary_sensor`, and any other entity exposing state through `ha_get_state`.

Brief binary pulses use `type: transition`. The engine queries Home Assistant history between the persisted `after`
cursor and an explicit end time, then advances the cursor atomically. The end time stays five seconds behind the
present by default (`settle_seconds`) so Recorder can persist very short pulses before the cursor advances. A sensor
may return to `off` before the worker runs without losing the `off -> on` activation, and recurring jobs do not
consume the same transition twice. An empty historical window never advances the cursor: if Recorder publishes a
transition late, the next pass still sees it. The cursor advances only after a matching transition was observed.

## Numeric sensor example

```json
{
  "version": 1,
  "name": "Low warehouse power",
  "conditions": [
    {"entity_id": "sensor.warehouse_power", "operator": "lt", "value": 500}
  ],
  "condition_policy": {"on_false": "reschedule", "delay_seconds": 60},
  "steps": [
    {
      "type": "service",
      "domain": "switch",
      "service": "turn_on",
      "target": {"entity_id": "switch.nspanel_relay_2"},
      "expected_state": "on"
    }
  ]
}
```

## Binary sensor example

```json
{
  "version": 1,
  "name": "Doorbell turns on entrance light",
  "conditions": [
    {
      "type": "transition",
      "entity_id": "binary_sensor.automatismosf2_rob32_tecladoopen",
      "from_state": "off",
      "to_state": "on",
      "after": "2026-07-16T06:00:00+00:00",
      "settle_seconds": 5,
      "operator": "gte",
      "value": 1
    }
  ],
  "condition_policy": {"on_false": "reschedule", "delay_seconds": 5},
  "steps": [
    {
      "type": "service",
      "domain": "switch",
      "service": "turn_on",
      "target": {"entity_id": "switch.nspanel_relay_2"},
      "expected_state": "on"
    }
  ]
}
```

## Extension rule

Add a new condition operator or step type only when Home Assistant behavior cannot be expressed by the existing
schema. Do not add `DETERMINISTIC_<SENSOR_NAME>` handlers. Entity aliases and natural-language compilers are input
adapters; they produce the same generic plan consumed by the shared executor.

Legacy `DETERMINISTIC_*` handlers remain temporarily so persisted tasks created by older versions can finish.
