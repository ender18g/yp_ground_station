"""Mission protocol and deployment regressions; no vehicle or server required."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion_vehicle_software.bundle_bridge import bundle_bridge
from yp_common import geometry, sar_missions as missions


ROOT = Path(__file__).resolve().parents[1]
MAV = missions.mavutil.mavlink


class MissionItemsTests(unittest.TestCase):
    def test_defaults_invalid_entries_and_explicit_parameters(self):
        waypoints = [
            None, {}, {"latitude": 1},
            {"latitude": "38.9", "longitude": "-76.4"},
            {"latitude": 39, "longitude": -76, "altitude": 40,
             "item_type": "takeoff", "command_id": MAV.MAV_CMD_DO_JUMP,
             "hold_time_s": 9, "acceptance_radius_m": 10, "yaw_deg": 90,
             "param1": 0, "param2": 2, "param3": 3, "param4": 4},
        ]
        self.assertEqual(missions.build_mission_items(waypoints), [
            (38.9, -76.4, 30.0, MAV.MAV_CMD_NAV_WAYPOINT, 0.0, 8.0, 0.0, 0.0),
            (39.0, -76.0, 40.0, MAV.MAV_CMD_DO_JUMP, 0.0, 2.0, 3.0, 4.0),
        ])

    def test_item_types_and_unknown_type_fallback(self):
        for item_type, command in [
            ("TAKEOFF", MAV.MAV_CMD_NAV_TAKEOFF),
            ("loiter_time", MAV.MAV_CMD_NAV_LOITER_TIME),
            ("land", MAV.MAV_CMD_NAV_LAND),
            ("rtl", MAV.MAV_CMD_NAV_RETURN_TO_LAUNCH),
            ("do_jump", MAV.MAV_CMD_DO_JUMP),
            ("unknown", MAV.MAV_CMD_NAV_WAYPOINT),
        ]:
            with self.subTest(item_type=item_type):
                item = missions.build_mission_items([
                    {"latitude": 0, "longitude": 0, "item_type": item_type}
                ])[0]
                self.assertEqual(item[3], command)

    def test_surface_altitude_and_final_guided_command(self):
        items = missions.build_mission_items(
            [{"latitude": 38.9, "longitude": -76.4, "altitude": 45}],
            surface_vehicle=True, force_guided_on_complete=True,
        )
        self.assertEqual(items[0][2], 0.0)
        self.assertEqual(items[1], (38.9, -76.4, 0.0, MAV.MAV_CMD_NAV_GUIDED_ENABLE, 1.0, 0.0, 0.0, 0.0))
        self.assertEqual(missions.build_mission_items([{}], force_guided_on_complete=True), [])

    def test_web_config_bridge_keeps_its_parameter_defaults(self):
        item = missions.build_mission_items([
            {"latitude": 1, "longitude": 2, "hold_time_s": 9,
             "acceptance_radius_m": 10, "yaw_deg": 90, "param1": 0}
        ], parameter_overrides=False)[0]
        self.assertEqual(item[4:], (9.0, 10.0, 0.0, 90.0))


class GeometryTests(unittest.TestCase):
    def test_ship_coordinates_rotate_with_heading_and_keep_relative_altitude(self):
        lat, lon, alt = geometry.relative_waypoint_to_global(
            38.9, -76.4, 90.0, 4.0, {"x": 10.0, "y": 20.0, "z": 30.0},
        )
        north, east = geometry.north_east_delta_m(38.9, -76.4, lat, lon)
        self.assertAlmostEqual(north, -10.0, delta=0.001)
        self.assertAlmostEqual(east, 20.0, delta=0.001)
        self.assertEqual(alt, 34.0)
        self.assertAlmostEqual(geometry.distance_m(38.9, -76.4, lat, lon), 500 ** 0.5, places=6)

    def test_grid_alternates_tracks_without_changing_altitude(self):
        grid = missions.calculate_search_grid_waypoints(38.9, -76.4, 100, 25, 30)
        self.assertEqual(len(grid), 8)
        self.assertTrue(all(alt == 30 for _, _, alt in grid))
        self.assertLess(grid[0][0], grid[1][0])
        self.assertGreater(grid[2][0], grid[3][0])
        self.assertEqual(grid[0][1], grid[1][1])
        self.assertLess(grid[1][1], grid[2][1])
        self.assertEqual(missions.calculate_search_grid_waypoints(0, 0, 100, 0, 30), [])

    def test_mob_keeps_center_lane_and_reverses_from_newest(self):
        track = [(38.9, -76.4), (38.901, -76.4), (38.902, -76.4)]
        oldest = missions.calculate_mob_waypoints(track, 20, 20, 30)
        newest = missions.calculate_mob_waypoints(track, 20, 20, 30, start_from_newest=True)
        self.assertEqual(oldest[:3], [(lat, lon, 30) for lat, lon in track])
        self.assertEqual(newest[:3], list(reversed(oldest[:3])))
        self.assertEqual(len(oldest), 9)
        self.assertEqual(missions.calculate_mob_waypoints(track[:1], 20, 20, 30), [])


class MissionProtocolTests(unittest.TestCase):
    def test_wait_for_ack_ignores_stale_command(self):
        expected = SimpleNamespace(command=MAV.MAV_CMD_MISSION_START, result=MAV.MAV_RESULT_ACCEPTED)
        master = Mock()
        master.recv_match.side_effect = [
            SimpleNamespace(command=MAV.MAV_CMD_DO_SET_MODE, result=0), expected,
        ]
        self.assertIs(missions.wait_for_command_ack(master, MAV.MAV_CMD_MISSION_START, 5), expected)
        self.assertEqual(master.recv_match.call_count, 2)

    def test_upload_sends_home_and_requested_mission_items(self):
        master = Mock(target_system=7, target_component=1)
        master.recv_match.side_effect = [
            SimpleNamespace(type=MAV.MAV_MISSION_ACCEPTED), None,
            SimpleNamespace(seq=0, get_type=lambda: "MISSION_REQUEST_INT"),
            SimpleNamespace(seq=1, get_type=lambda: "MISSION_REQUEST_INT"),
            SimpleNamespace(type=MAV.MAV_MISSION_ACCEPTED),
        ]
        items = missions.build_mission_items([{"latitude": 38.9, "longitude": -76.4, "altitude": 30}])
        with patch.object(missions.time, "sleep"):
            self.assertTrue(missions.upload_mission(master, items))
        master.mav.mission_count_send.assert_called_once_with(7, 1, 2, MAV.MAV_MISSION_TYPE_MISSION)
        home, waypoint = [call.args[0] for call in master.mav.send.call_args_list]
        self.assertEqual((home.seq, home.x, home.y, home.z), (0, 389000000, -764000000, 0))
        self.assertEqual((waypoint.seq, waypoint.command, waypoint.z), (1, MAV.MAV_CMD_NAV_WAYPOINT, 30))

    def test_upload_rejection_does_not_report_success(self):
        master = Mock(target_system=7, target_component=1)
        master.recv_match.side_effect = [
            SimpleNamespace(type=MAV.MAV_MISSION_ACCEPTED), None,
            SimpleNamespace(get_type=lambda: "MISSION_ACK"),
        ]
        items = missions.build_mission_items([{"latitude": 38.9, "longitude": -76.4}])
        with patch.object(missions.time, "sleep"):
            self.assertFalse(missions.upload_mission(master, items))
        master.mav.send.assert_not_called()

    def test_surface_stream_keeps_telemetry_and_ignores_altitude_error(self):
        master = Mock(target_system=7, target_component=1)
        position = SimpleNamespace(lat=389000000, lon=-764000000, relative_alt=100000)
        master.recv_match.return_value = position
        callback = Mock()
        with patch.object(missions, "set_mode"), patch.object(missions, "arm_vehicle", return_value=True), patch.object(missions.time, "sleep"):
            self.assertTrue(missions.stream_waypoints_guided(
                master, [(38.9, -76.4, 30)], telemetry_callback=callback,
            ))
        callback.assert_called_once_with(position)
        sent = master.mav.set_position_target_global_int_send.call_args.args
        self.assertEqual(sent[5:8], (389000000, -764000000, 0.0))

    def test_cancelled_stream_does_not_send_waypoint(self):
        master = Mock(target_system=7, target_component=1)
        stop = threading.Event()
        stop.set()
        with patch.object(missions, "set_mode"), patch.object(missions, "arm_vehicle", return_value=True), patch.object(missions.time, "sleep"):
            self.assertFalse(missions.stream_waypoints_guided(master, [(38.9, -76.4, 0)], stop_event=stop))
        master.mav.set_position_target_global_int_send.assert_not_called()


class DeploymentTests(unittest.TestCase):
    def assert_import_from(self, directory: Path, canonical: Path, bridge: str | None = None):
        code = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import sar_missions
assert Path(sar_missions.__file__).resolve() == Path(sys.argv[2]).resolve()
assert sar_missions._haversine_m(0, 0, 0, 0) == 0
if len(sys.argv) > 3:
    import importlib
    module = importlib.import_module(sys.argv[3])
    assert module.sar_missions is sar_missions
    assert module._relative_waypoint_to_global(0, 0, 0, 2, {"z": 3}) == (0, 0, 5)
"""
        env = {**os.environ, "WEBRTC_IP": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        args = [sys.executable, "-c", code, str(directory), str(canonical)]
        if bridge:
            args.append(bridge)
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_imports_work_outside_repository_working_directory(self):
        for directory in (
            "services/server", "services/arducopter_ws_bridge",
            "companion_vehicle_software/arducopter_piScripts",
            "companion_vehicle_software/blueboat_piScripts",
        ):
            with self.subTest(directory=directory):
                self.assert_import_from(ROOT / directory, ROOT / "yp_common/sar_missions.py")

    def test_docker_copy_layout_finds_shared_module(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp)
            shutil.copytree(ROOT / "yp_common", app / "yp_common")
            shutil.copy(ROOT / "services/server/sar_missions.py", app)
            self.assert_import_from(app, app / "yp_common/sar_missions.py")

    def test_pi_bundles_work_without_repository_on_import_path(self):
        for bridge in ("arducopter", "blueboat"):
            with self.subTest(bridge=bridge), tempfile.TemporaryDirectory() as temp:
                bundle = bundle_bridge(bridge, Path(temp) / bridge)
                self.assert_import_from(bundle, bundle / "yp_common/sar_missions.py", f"{bridge}_bridge")
                self.assertTrue((bundle / "requirements.txt").is_file())
                with self.assertRaises(FileExistsError):
                    bundle_bridge(bridge, bundle)


if __name__ == "__main__":
    unittest.main()
