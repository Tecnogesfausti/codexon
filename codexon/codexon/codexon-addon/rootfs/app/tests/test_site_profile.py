from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from site_profile import SiteProfile, SiteProfileError


class SiteProfileTest(unittest.TestCase):
    def test_missing_profile_is_empty(self) -> None:
        profile = SiteProfile.load(Path("/missing/codexon-site-profile.yaml"))

        self.assertEqual(profile.roles, {})
        self.assertIsNone(profile.resolve_alias("kitchen light"))

    def test_loads_roles_and_resolves_longest_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "site.yaml"
            path.write_text(
                """
version: 1
roles:
  lighting.room:
    entity_id: light.room
    label: room light
    aliases: [light]
  lighting.kitchen:
    entity_id: switch.kitchen
    label: kitchen light
    aliases: [light in the kitchen, kitchen light]
instructions:
  - Keep the water valve closed by default.
""",
                encoding="utf-8",
            )

            profile = SiteProfile.load(path)

        self.assertEqual(profile.entity("lighting.kitchen"), "switch.kitchen")
        self.assertEqual(
            profile.resolve_alias("Please turn on the light in the kitchen"),
            ("switch.kitchen", "kitchen light"),
        )
        self.assertIn("Keep the water valve closed", profile.prompt_context())

    def test_rejects_roles_without_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "site.yaml"
            path.write_text("version: 1\nroles:\n  climate.primary: {}\n", encoding="utf-8")

            with self.assertRaises(SiteProfileError):
                SiteProfile.load(path)
