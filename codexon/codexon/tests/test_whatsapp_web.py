import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codexon_web


class WhatsAppWebStatusTest(unittest.TestCase):
    def test_status_exposes_pairing_qr_from_existing_panel(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (data_dir / "status.json").write_text(
                json.dumps(
                    {
                        "state": "qr",
                        "hasQr": True,
                        "messageCount": 3,
                        "updatedAt": "2026-07-29T09:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "qr-data-url.txt").write_text(
                "data:image/png;base64,AAAA\n", encoding="utf-8"
            )
            with (
                patch.object(codexon_web, "WHATSAPP_DATA_DIR", data_dir),
                patch.dict(os.environ, {"CODEXON_WHATSAPP_ENABLED": "true"}),
            ):
                status = codexon_web.api_whatsapp()

        self.assertTrue(status["enabled"])
        self.assertEqual(status["state"], "qr")
        self.assertEqual(status["messageCount"], 3)
        self.assertEqual(status["qrDataUrl"], "data:image/png;base64,AAAA")

    def test_missing_status_is_reported_without_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(codexon_web, "WHATSAPP_DATA_DIR", Path(temporary)),
                patch.dict(
                    os.environ,
                    {"CODEXON_WHATSAPP_ENABLED": "false"},
                ),
            ):
                status = codexon_web.whatsapp_status(include_qr=True)

        self.assertFalse(status["enabled"])
        self.assertEqual(status["state"], "disabled")
        self.assertIsNone(status["qrDataUrl"])
