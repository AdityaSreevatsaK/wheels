import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts import migrate_from_tracker


class Migrate(unittest.TestCase):
    def index(self, tmp, data):
        entry = {"wheel": "x-1.0-cp314-cp314-win_arm64.whl", "sha256": hashlib.sha256(data).hexdigest()}
        (tmp / "gap-wheels.json").write_text(json.dumps({"wheels": [entry]}))
        return tmp / "gap-wheels.json"

    def test_keeps_a_wheel_whose_sha256_matches_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            urls = []
            n = migrate_from_tracker.migrate(self.index(tmp, b"wheel"), tmp / "out", lambda u: urls.append(u) or b"wheel")
            self.assertEqual(n, 1)
            self.assertEqual((tmp / "out" / "x-1.0-cp314-cp314-win_arm64.whl").read_bytes(), b"wheel")
            self.assertEqual(urls, [migrate_from_tracker.TRACKER_RELEASE + "x-1.0-cp314-cp314-win_arm64.whl"])

    def test_any_mismatch_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with self.assertRaises(ValueError):
                migrate_from_tracker.migrate(self.index(tmp, b"wheel"), tmp / "out", lambda u: b"other bytes")
            self.assertFalse((tmp / "out" / "x-1.0-cp314-cp314-win_arm64.whl").exists())
