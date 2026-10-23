"""Optional dashboard names preserve the remaining settings contract."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import settings as cfg


class SettingsTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "outputs"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="settings-tests-", dir=root)
        self.addCleanup(self.temp.cleanup)
        self.enterContext(patch.object(cfg, "PATH", str(Path(self.temp.name) / "settings.json")))

    def test_blank_names_save_and_reload(self):
        defaults = copy.deepcopy(cfg.DEFAULTS)
        cfg.save({"station_name": "Quezon Ave - Platform A", "line_name": "Line 1"})
        saved = cfg.save({"station_name": "   ", "line_name": ""})
        self.assertEqual(saved["station_name"], "")
        self.assertEqual(saved["line_name"], "")
        self.assertEqual(cfg.load(), saved)
        self.assertEqual(cfg.DEFAULTS, defaults)

    def test_names_are_stripped(self):
        saved = cfg.save({"station_name": "  Quezon Ave - Platform A  ", "line_name": "  Line 1  "})
        self.assertEqual(saved["station_name"], "Quezon Ave - Platform A")
        self.assertEqual(saved["line_name"], "Line 1")

    def test_names_are_capped_at_sixty_characters(self):
        saved = cfg.save({"station_name": "  " + "Station " * 12 + "  ", "line_name": "  " + "Line " * 16 + "  "})
        self.assertEqual(saved["station_name"], ("Station " * 12).strip()[:60])
        self.assertEqual(saved["line_name"], ("Line " * 16).strip()[:60])
        self.assertEqual(len(saved["station_name"]), 60)
        self.assertEqual(len(saved["line_name"]), 60)

    def test_omitted_names_and_other_settings_are_preserved(self):
        current = cfg.save({"station_name": "Custom Station", "line_name": "Custom Line", "refresh_s": 12})
        self.assertEqual(cfg.save({"refresh_s": 12}), current)
        expected = dict(current, station_name="", line_name="")
        self.assertEqual(cfg.save({"station_name": "", "line_name": ""}), expected)
        self.assertEqual(cfg.save({"language": "en"}), dict(expected, language="en"))

    def test_other_validation_still_rejects_invalid_settings(self):
        current = cfg.save({"station_name": "", "line_name": ""})
        for invalid in ({"moderate_at": 0}, {"high_at": 101}, {"moderate_at": 50, "high_at": 50},
                        {"alert_lead_min": 4}, {"refresh_s": 1}, {"retention_days": 6},
                        {"language": "invalid"}, {"active_model": "invalid"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    cfg.save(invalid)
                self.assertEqual(cfg.load(), current)

    def test_names_can_be_cleared_through_pin_protected_save(self):
        import app

        cfg.save({"station_name": "Custom Station", "line_name": "Custom Line"})
        client = app.app.test_client()
        payload = {"settings": {"station_name": "  ", "line_name": ""}}
        with patch.object(app, "ADMIN_PIN", "test-pin"), patch.object(app._state_builder, "notify"):
            rejected = client.post("/api/settings", json=payload, headers={"Host": "127.0.0.1"})
            self.assertEqual(rejected.status_code, 403)
            self.assertEqual(cfg.load()["station_name"], "Custom Station")
            accepted = client.post("/api/settings", json=dict(payload, pin="test-pin"), headers={"Host": "127.0.0.1"})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.get_json()["settings"]["station_name"], "")
        self.assertEqual(accepted.get_json()["settings"]["line_name"], "")
        self.assertEqual(cfg.load()["station_name"], "")
        self.assertEqual(cfg.load()["line_name"], "")

    def test_names_do_not_change_display_bytes(self):
        import app

        snapshot = copy.deepcopy(app._state_builder.get())
        snapshot["state"].update(station="Custom Station", line="Custom Line")
        client = app.app.test_client()
        with patch.object(app._state_builder, "get", return_value=snapshot):
            before = client.get("/api/display", headers={"Host": "127.0.0.1"})
            snapshot["state"].update(station="", line="")
            after = client.get("/api/display", headers={"Host": "127.0.0.1"})
        self.assertEqual(before.status_code, 200)
        self.assertEqual(after.status_code, 200)
        self.assertEqual(before.get_data(), after.get_data())


if __name__ == "__main__":
    unittest.main()
