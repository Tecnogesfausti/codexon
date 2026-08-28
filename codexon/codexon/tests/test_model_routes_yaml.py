from pathlib import Path
import unittest

import yaml


class ModelRoutesYamlTest(unittest.TestCase):
    def test_model_routes_is_valid_yaml(self) -> None:
        path = Path(__file__).resolve().parents[1] / "model_routes.yaml"
        config = yaml.safe_load(path.read_text(encoding="utf-8"))

        self.assertIsInstance(config, dict)
        self.assertIsInstance(config.get("routes"), dict)
        self.assertIn("homeassistant", config["routes"])
        self.assertIn("image_analysis", config["routes"])
        self.assertEqual(config["routes"]["image_analysis"].get("fallbacks"), [])


if __name__ == "__main__":
    unittest.main()
