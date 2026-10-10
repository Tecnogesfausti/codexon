import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
import codexon_web
from services.security_visual import inconclusive


class SecurityVisualWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.db = patch.object(codexon_web, "DB_PATH", root / "memory.sqlite3")
        self.profile = patch.object(codexon_web, "SITE_PROFILE_PATH", root / "site.yaml")
        self.db.start()
        self.profile.start()
        conn = codexon_web.connect_db()
        cursor = conn.execute("INSERT INTO security_visual_cases(source_key,camera,detected_at,received_at,candidate_json,result_json,status) VALUES(?,?,?,?,?,?,?)", ("test", "casa", codexon_web.utc_now(), codexon_web.utc_now(), json.dumps({"sources": ["tapo"]}), json.dumps(inconclusive("1", "Imagen actual")), "reviewed"))
        self.case_id = cursor.lastrowid
        conn.commit()
        conn.close()

    def tearDown(self):
        self.profile.stop()
        self.db.stop()
        self.directory.cleanup()

    def test_status_labels_and_invalid_labels(self):
        result = codexon_web.api_security_visual()
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["totals"]["reviewed"], 1)
        self.assertEqual(result["usage"]["calls"], 0)
        codexon_web.api_security_label(self.case_id, {"label": "person"})
        self.assertEqual(codexon_web.api_security_visual()["labels"], {"person": 1})
        for case_id, payload in [(self.case_id, {"label": "allow"}), (999, {"label": "person"})]:
            with self.assertRaises(HTTPException):
                codexon_web.api_security_label(case_id, payload)

    def test_image_route_rejects_external_paths_and_wrong_case(self):
        outside = Path(self.directory.name) / "private.jpg"
        outside.write_bytes(b"private")
        conn = codexon_web.connect_db()
        review_id = conn.execute("INSERT INTO security_visual_reviews(case_id,started_at,frames_json) VALUES(?,?,?)", (self.case_id, codexon_web.utc_now(), json.dumps([{"path": str(outside), "media_type": "image/jpeg"}]))).lastrowid
        conn.commit()
        conn.close()
        with self.assertRaises(HTTPException):
            codexon_web.api_security_image(self.case_id, review_id, 0)
        with self.assertRaises(HTTPException):
            codexon_web.api_security_image(999, review_id, 0)
        images = Path(self.directory.name) / "security_visual_images"
        images.mkdir()
        inside = images / "frame.jpg"
        inside.write_bytes(b"image")
        conn = codexon_web.connect_db()
        conn.execute("UPDATE security_visual_reviews SET frames_json=? WHERE id=?", (json.dumps([{"path": str(inside), "media_type": "image/jpeg"}]), review_id))
        conn.commit()
        conn.close()
        response = codexon_web.api_security_image(self.case_id, review_id, 0)
        self.assertEqual(Path(response.path), inside)
