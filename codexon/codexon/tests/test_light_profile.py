from __future__ import annotations

import unittest

from planning.light import format_light_sensation_answer


class LightProfileTest(unittest.TestCase):
    def test_formats_configured_profile_entities(self) -> None:
        answer = format_light_sensation_answer(
            [
                ("sensor.custom_inside", {"state": "42", "attributes": {"unit_of_measurement": "lx"}}),
                ("sensor.custom_outside", {"state": "650", "attributes": {"unit_of_measurement": "lx"}}),
            ],
            interior_entity="sensor.custom_inside",
            exterior_entity="sensor.custom_outside",
        )

        self.assertIn("42,0lx", answer)
        self.assertIn("650,0lx", answer)
        self.assertIn("sensor.custom_inside", answer)
        self.assertNotIn("sensor.muralcocina", answer)
