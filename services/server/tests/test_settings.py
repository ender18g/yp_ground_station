import json
import unittest
from unittest.mock import patch

from support import DatabaseTestCase

from app import auth, main, settings
from app.deconfliction import DEFAULT_DECONFLICT_RADIUS_M, DeconflictionEngine


class SettingsTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.original_runtime = main.settings.copy()
        self.original_role = main._yp_role_vehicle_id
        self.addCleanup(self.restore_runtime)

    def restore_runtime(self):
        main.settings.clear()
        main.settings.update(self.original_runtime)
        main._yp_role_vehicle_id = self.original_role

    def test_rejected_update_changes_neither_persistence_nor_runtime(self):
        original = settings.get_application_settings()
        runtime = main.settings.copy()
        for invalid in ({"message_retention_seconds": 10}, {"rtb_update_hz": 21},
                        {"mob_altitude_m": "NaN"}, {"rtk_network_port": 1.5},
                        {"rtk_source_type": "unknown"}):
            with self.subTest(invalid=invalid):
                response = self.client.put("/api/settings", json={"trail_seconds": 90, **invalid})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(settings.get_application_settings(), original)
                self.assertEqual(main.settings, runtime)

    def test_rtk_settings_are_persisted_and_runtime_values_are_normalized(self):
        response = self.client.put("/api/settings", json={
            "rtk_source_type": "tcp", "rtk_host_or_port": "  base.example  ",
            "rtk_network_port": "2101", "rtk_baudrate": "57600",
            "mob_altitude_m": "45.5", "show_yp_range_rings": False,
            "yp_role_vehicle_id": "  boat-01  ",
        })
        self.assertEqual(response.status_code, 200, response.text)
        persisted = settings.get_application_settings()
        self.assertEqual(persisted["rtk_source_type"], "tcp")
        self.assertEqual(persisted["rtk_host_or_port"], "base.example")
        self.assertEqual(persisted["rtk_network_port"], 2101)
        self.assertEqual(persisted["rtk_baudrate"], 57600)
        self.assertEqual(persisted["mob_altitude_m"], 45.5)
        self.assertFalse(persisted["show_yp_range_rings"])
        self.assertEqual(self.client.get("/api/yp/role").json(), {"vehicle_id": "boat-01"})
        for key, value in persisted.items():
            self.assertEqual(main.settings[key], value)
        self.assertEqual(self.client.get("/api/settings").json(), response.json())

    def test_deconfliction_update_replaces_removed_overrides(self):
        with patch.object(main, "deconfliction_engine", DeconflictionEngine()):
            response = self.client.put("/api/deconfliction/settings", json={
                "enabled": True, "radius_per_type": {"uav": 80, "custom": 90},
            })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(main.deconfliction_engine.get_radius("uav"), 80)
            response = self.client.put("/api/deconfliction/settings", json={"radius_per_type": {}})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(main.deconfliction_engine.get_radius("uav"), DEFAULT_DECONFLICT_RADIUS_M["uav"])
            self.assertEqual(main.deconfliction_engine.get_radius("custom"), 10)

    def test_invalid_deconfliction_radius_does_not_partially_commit(self):
        original = settings.get_deconfliction_settings()
        for value in ([], {"uav": "NaN"}, {"uav": -1}):
            with self.subTest(value=value):
                response = self.client.put("/api/deconfliction/settings", json={
                    "enabled": True, "global_radius_m": 50, "radius_per_type": value,
                })
                self.assertEqual(response.status_code, 400)
                self.assertEqual(settings.get_deconfliction_settings(), original)

    def test_legacy_invalid_radius_overrides_do_not_replace_defaults(self):
        with auth.get_db_session() as session:
            session.add(settings.DeconflictionSettings(radius_per_type_json=json.dumps({
                "uav": -1, "usv": None, "uuv": "NaN", "ugv": 0, "custom": 42,
            })))
            session.commit()
        with patch.object(main, "deconfliction_engine", DeconflictionEngine()):
            main._apply_deconfliction_settings(settings.get_deconfliction_settings())
            for vehicle_type in ("uav", "usv", "uuv", "ugv"):
                self.assertEqual(main.deconfliction_engine.get_radius(vehicle_type), DEFAULT_DECONFLICT_RADIUS_M[vehicle_type])
            self.assertEqual(main.deconfliction_engine.get_radius("custom"), 42)

    def test_vehicle_type_changes_apply_to_deconfliction_priority(self):
        engine = DeconflictionEngine(enabled=True)
        position = {"latitude": 38.9, "longitude": -76.4, "altitude": 0}
        engine.update_vehicle("boat", "usv", position)
        engine.update_vehicle("drone", "uav", position, "rtb")
        self.assertEqual(len(engine.detect_conflicts()), 1)
        engine.update_vehicle("boat", "yp", position)
        self.assertEqual(engine.detect_conflicts(), [])


if __name__ == "__main__":
    unittest.main()
