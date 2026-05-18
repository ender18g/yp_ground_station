"""MAVLink controller: connect, manage modes, upload missions, and navigate vehicles."""
from __future__ import annotations

import math
import time
import logging
import threading
from pymavlink import mavutil, mavwp

EARTH_RADIUS_METERS = 6_378_137.0


class MavlinkController:
    """Controls a single vehicle via MAVLink: connection, mode, missions, and movement."""

    def __init__(
        self,
        connection_string: str,
        baudrate: int | None = None,
        source_system_id: int = 255,
    ):
        self.logger = logging.getLogger(__name__)
        self.connection_string = connection_string
        self.baudrate = baudrate
        self.source_system_id = source_system_id
        self.master = None
        # Set while a mission-sequence thread owns this controller so the poll
        # loop does not consume COMMAND_ACKs meant for set_mode / arm_vehicle.
        self._mavlink_busy = threading.Event()

    def connect(self) -> None:
        """Establish a MAVLink connection and wait for a valid heartbeat."""
        try:
            is_serial = (
                "com" in self.connection_string.lower()
                or "/dev/tty" in self.connection_string.lower()
            )
            if is_serial:
                self.master = mavutil.mavlink_connection(
                    self.connection_string,
                    baud=self.baudrate,
                    source_system=self.source_system_id,
                )
            else:
                self.master = mavutil.mavlink_connection(
                    self.connection_string,
                    source_system=self.source_system_id,
                    dialect="ardupilotmega",
                    autoreconnect=True,
                    use_native=False,
                    force_connected=True,
                    mavlink2=True,
                )
            self.logger.info(
                f"MAVLink: waiting for heartbeat on {self.connection_string} (timeout 10s)…"
            )
            self.master.wait_heartbeat(timeout=10)
            if self.master.target_system == 0:
                self.master = None
                raise RuntimeError(
                    f"Invalid heartbeat (system=0) for {self.connection_string}"
                )
            self.logger.info(
                f"MAVLink: heartbeat from system {self.master.target_system} "
                f"component {self.master.target_component}."
            )
            self.master.target_component = 1
            self.start_position_stream()
        except Exception as exc:
            self.master = None
            raise RuntimeError(
                f"MAVLink connection failed for {self.connection_string}: {exc}"
            ) from exc

    def is_connected(self) -> bool:
        return self.master is not None

    def start_position_stream(self, rate_hz: int = 2) -> None:
        """Request GLOBAL_POSITION_INT streaming at rate_hz from the vehicle."""
        if not self.is_connected():
            return
        interval_us = int(1_000_000 / rate_hz)
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0,
                mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT,
                interval_us,
                0, 0, 0, 0, 0,
            )
            self.master.mav.request_data_stream_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_POSITION,
                max(1, int(rate_hz)),
                1,
            )
            self.master.mav.request_data_stream_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_EXTRA1,
                max(1, int(rate_hz)),
                1,
            )
        except Exception as exc:
            self.logger.error(f"Failed to request position stream: {exc}")

    def get_current_position(self) -> dict | None:
        """Return the vehicle's current position as {lat, lon, alt} or None."""
        if not self.is_connected():
            return None
        try:
            self.master.mav.command_long_send(
                self.master.target_system,
                self.master.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL,
                0,
                mavutil.mavlink.MAVLINK_MSG_ID_GLOBAL_POSITION_INT,
                200_000,
                0, 0, 0, 0, 0,
            )
        except Exception:
            pass
        start = time.time()
        msg = None
        while time.time() - start < 2.0:
            try:
                msg = self.master.messages.get("GLOBAL_POSITION_INT")
            except Exception:
                msg = None
            if msg:
                break
            time.sleep(0.1)
        if not msg:
            return None
        return {
            "lat": msg.lat / 1e7,
            "lon": msg.lon / 1e7,
            "alt": msg.relative_alt / 1000.0,
        }

    def upload_mission(self, waypoints_data: list) -> bool:
        """Clear the vehicle's mission and upload the supplied waypoints."""
        if not self.is_connected():
            self.logger.error("MAVLink: not connected — cannot upload mission.")
            return False
        self.master.target_component = 1
        wp_loader = mavwp.MAVWPLoader()
        seq = 0

        # Seq 0 is always a home waypoint (taken from the first data point).
        home_lat, home_lon = waypoints_data[0][0], waypoints_data[0][1]
        home_item = mavutil.mavlink.MAVLink_mission_item_int_message(
            self.master.target_system, 1, seq,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
            0, 1, 0, 0, 0, 0,
            int(home_lat * 1e7), int(home_lon * 1e7), 0.0,
        )
        wp_loader.add(home_item)
        seq += 1

        for item in waypoints_data:
            if len(item) == 8:
                lat, lon, alt, cmd_id, p1, p2, p3, p4 = item
                frame = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT
                is_current, autocontinue = 0, 1
            elif len(item) == 11:
                lat, lon, alt, cmd_id, p1, p2, p3, p4, frame, is_current, autocontinue = item
            else:
                continue
            wp_loader.add(
                mavutil.mavlink.MAVLink_mission_item_int_message(
                    self.master.target_system, 1, seq,
                    frame, cmd_id, is_current, autocontinue,
                    p1, p2, p3, p4,
                    int(lat * 1e7), int(lon * 1e7), float(alt),
                )
            )
            seq += 1

        if wp_loader.count() == 0:
            self.logger.error("MAVLink: no valid waypoints to upload.")
            return False
        try:
            self.master.mav.mission_clear_all_send(self.master.target_system, 1)
            self.master.recv_match(type="MISSION_ACK", blocking=True, timeout=5)
            while self.master.recv_match(blocking=False):
                pass
            time.sleep(0.5)
            self.master.mav.mission_count_send(
                self.master.target_system, 1,
                wp_loader.count(),
                mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
            )
            for i in range(wp_loader.count()):
                msg = self.master.recv_match(
                    type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
                    blocking=True,
                    timeout=10,
                )
                if not msg:
                    self.logger.error(f"MAVLink: no MISSION_REQUEST for wp {i} (timeout).")
                    return False
                if msg.get_type() == "MISSION_ACK":
                    self.logger.error(
                        f"MAVLink: unexpected MISSION_ACK at wp {i} — vehicle rejected mission."
                    )
                    return False
                self.master.mav.send(wp_loader.wp(msg.seq))
                if msg.seq == wp_loader.count() - 1:
                    break
            final_ack = self.master.recv_match(type="MISSION_ACK", blocking=True, timeout=10)
            if final_ack and final_ack.type == mavutil.mavlink.MAV_MISSION_ACCEPTED:
                self.logger.info("MAVLink: mission upload successful.")
                return True
            self.logger.error(f"MAVLink: mission upload failed — final ACK: {final_ack}")
            return False
        except Exception as exc:
            self.logger.error(f"MAVLink: mission upload error: {exc}")
            return False

    def set_mode(self, mode_name: str) -> bool:
        """Set the vehicle flight mode and wait for a COMMAND_ACK."""
        if not self.is_connected():
            return False
        mode_mapping = self.master.mode_mapping()
        if mode_mapping is None or mode_name.upper() not in mode_mapping:
            self.logger.error(f"MAVLink: unknown mode '{mode_name}'.")
            return False
        mode_id = mode_mapping[mode_name.upper()]
        try:
            self.master.mav.set_mode_send(
                self.master.target_system,
                mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                mode_id,
            )
            ack = self.master.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
            if ack and ack.command == mavutil.mavlink.MAV_CMD_DO_SET_MODE:
                if ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                    self.logger.info(f"MAVLink: mode {mode_name} confirmed.")
                    return True
                self.logger.error(
                    f"MAVLink: mode {mode_name} rejected (result={ack.result})."
                )
                return False
            # No ACK within timeout — assume success for physical hardware.
            self.logger.warning(
                f"MAVLink: no ACK for mode {mode_name} — assuming success."
            )
            return True
        except Exception as exc:
            self.logger.error(f"MAVLink: error setting mode: {exc}")
            return False

    def arm_vehicle(self, force: bool = False) -> bool:
        """Arm the vehicle. Set force=True to bypass pre-arm checks (e.g. MOB)."""
        if not self.is_connected():
            return False
        param2 = 21196.0 if force else 0.0
        self.master.mav.command_long_send(
            self.master.target_system, self.master.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
            0, 1, param2, 0, 0, 0, 0, 0,
        )
        ack = self.master.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
        if (
            ack
            and ack.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM
            and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
        ):
            self.logger.info(f"MAVLink: armed (force={force}).")
            return True
        self.logger.error(f"MAVLink: arm failed: {ack}")
        return False

    def start_mission(self, retries: int = 3, retry_delay: float = 1.5) -> bool:
        """Send MISSION_START and retry up to retries times on failure."""
        if not self.is_connected():
            return False
        for attempt in range(1, retries + 1):
            self.master.mav.command_long_send(
                self.master.target_system, self.master.target_component,
                mavutil.mavlink.MAV_CMD_MISSION_START,
                0, 0, 0, 0, 0, 0, 0, 0,
            )
            ack = self.master.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
            if (
                ack
                and ack.command == mavutil.mavlink.MAV_CMD_MISSION_START
                and ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
            ):
                self.logger.info("MAVLink: mission start accepted.")
                return True
            self.logger.warning(
                f"MAVLink: mission start attempt {attempt}/{retries} failed: {ack}."
                + (f" Retrying in {retry_delay}s…" if attempt < retries else "")
            )
            if attempt < retries:
                time.sleep(retry_delay)
        self.logger.error("MAVLink: mission start failed after all retries.")
        return False

    def fly_to(self, lat: float, lon: float, altitude_m: float) -> bool:
        """Command GUIDED-mode flight to a specific lat/lon/altitude."""
        if not self.is_connected():
            return False
        if not self.set_mode("GUIDED"):
            return False
        try:
            type_mask = (
                mavutil.mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_IGNORE
                | mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
            )
            self.master.mav.set_position_target_global_int_send(
                0,
                self.master.target_system, self.master.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                type_mask,
                int(lat * 1e7), int(lon * 1e7), float(altitude_m),
                0, 0, 0, 0, 0, 0, 0, 0,
            )
            return True
        except Exception as exc:
            self.logger.error(f"MAVLink: fly-to error: {exc}")
            return False

    def wait_for_waypoint_reached(
        self, waypoint_seq: int, timeout_seconds: float = 120
    ) -> bool:
        """Block until MISSION_ITEM_REACHED reports waypoint_seq, or timeout."""
        if not self.is_connected():
            return False
        start = time.time()
        while time.time() - start < timeout_seconds and self.is_connected():
            msg = (
                self.master.messages.get("MISSION_ITEM_REACHED")
                if hasattr(self.master, "messages")
                else None
            )
            if msg and getattr(msg, "seq", None) == waypoint_seq:
                return True
            time.sleep(0.5)
        return False

    def send_status_text(
        self, text: str, severity=None
    ) -> bool:
        """Send a MAVLink STATUSTEXT message to the vehicle."""
        if not self.is_connected():
            return False
        if severity is None:
            severity = mavutil.mavlink.MAV_SEVERITY_INFO
        payload = (text.encode("utf-8") if isinstance(text, str) else text)[:49]
        try:
            self.master.mav.statustext_send(severity, payload)
            return True
        except Exception as exc:
            self.logger.error(f"MAVLink: STATUSTEXT error: {exc}")
            return False

    def close_connection(self) -> None:
        if self.master:
            self.master.close()
            self.logger.info(f"MAVLink: connection closed for {self.connection_string}.")

    # ------------------------------------------------------------------
    # Grid-search mission generation
    # ------------------------------------------------------------------

    def _calculate_rectangular_grid_waypoints(
        self,
        center_lat: float,
        center_lon: float,
        grid_width_m: float,
        grid_height_m: float,
        swath_width_m: float,
        altitude_m: float,
    ) -> list[tuple]:
        """Return a boustrophedon list of (lat, lon, alt) waypoints for a grid search."""
        waypoints = []
        if swath_width_m <= 0 or grid_width_m <= 0 or grid_height_m <= 0:
            return waypoints
        clat_r = math.radians(center_lat)
        clon_r = math.radians(center_lon)
        south_lat_r = clat_r - grid_height_m / 2.0 / EARTH_RADIUS_METERS
        north_lat_r = clat_r + grid_height_m / 2.0 / EARTH_RADIUS_METERS
        west_lon_r = clon_r - (grid_width_m / 2.0) / (EARTH_RADIUS_METERS * math.cos(clat_r))
        num_tracks = max(1, int(math.floor(grid_width_m / swath_width_m)))
        step_r = swath_width_m / (EARTH_RADIUS_METERS * math.cos(clat_r))
        south_lat = math.degrees(south_lat_r)
        north_lat = math.degrees(north_lat_r)
        for i in range(num_tracks):
            lon_deg = math.degrees(west_lon_r + i * step_r)
            if i % 2 == 0:
                waypoints += [(south_lat, lon_deg, altitude_m), (north_lat, lon_deg, altitude_m)]
            else:
                waypoints += [(north_lat, lon_deg, altitude_m), (south_lat, lon_deg, altitude_m)]
        return waypoints

    def generate_and_upload_search_grid_mission(
        self,
        center_lat: float,
        center_lon: float,
        grid_size_m: float,
        swath_width_m: float,
        altitude_m: float,
        include_takeoff: bool = False,
        takeoff_altitude_m: float | None = None,
        climb_speed_ms: float = 8.0,
    ) -> list[tuple] | None:
        """Generate a boustrophedon grid mission, upload it, and return path positions."""
        if not self.is_connected():
            return None
        grid_wps = self._calculate_rectangular_grid_waypoints(
            center_lat, center_lon, grid_size_m, grid_size_m, swath_width_m, altitude_m
        )
        if not grid_wps:
            self.logger.error("MAVLink: failed to generate grid waypoints.")
            return None

        upload_wps: list = []
        if include_takeoff:
            ta = takeoff_altitude_m if takeoff_altitude_m is not None else altitude_m
            pos = self.get_current_position()
            tlat = pos["lat"] if pos else center_lat
            tlon = pos["lon"] if pos else center_lon
            upload_wps = [
                (tlat, tlon, 0.0, mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                 2.0, climb_speed_ms, -1.0, 0.0),
                (tlat, tlon, ta, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                 0.0, 0.0, 0.0, float("nan")),
            ]

        path: list[tuple] = []
        for lat, lon, alt in grid_wps:
            upload_wps.append(
                (lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                 0.0, 10.0, 0.0, float("nan"))
            )
            path.append((lat, lon))

        return path if self.upload_mission(upload_wps) else None

    # ------------------------------------------------------------------
    # MOB curved-track mission generation
    # ------------------------------------------------------------------

    @staticmethod
    def _offset_position(
        lat: float, lon: float, bearing_deg: float, distance_m: float
    ) -> tuple[float, float]:
        """Return (lat, lon) after travelling distance_m on bearing_deg from (lat, lon)."""
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        b_r = math.radians(bearing_deg)
        d = distance_m / EARTH_RADIUS_METERS
        lat2 = math.asin(
            math.sin(lat_r) * math.cos(d)
            + math.cos(lat_r) * math.sin(d) * math.cos(b_r)
        )
        lon2 = lon_r + math.atan2(
            math.sin(b_r) * math.sin(d) * math.cos(lat_r),
            math.cos(d) - math.sin(lat_r) * math.sin(lat2),
        )
        return math.degrees(lat2), math.degrees(lon2)

    @staticmethod
    def _bearing_between(
        lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
        """Forward azimuth in degrees from (lat1, lon1) to (lat2, lon2)."""
        la1, la2 = math.radians(lat1), math.radians(lat2)
        dlon = math.radians(lon2 - lon1)
        y = math.sin(dlon) * math.cos(la2)
        x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dlon)
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    @staticmethod
    def _mean_bearing(b1: float, b2: float) -> float:
        """Circular mean of two bearings, handling the 0/360 wrap."""
        x = math.cos(math.radians(b1)) + math.cos(math.radians(b2))
        y = math.sin(math.radians(b1)) + math.sin(math.radians(b2))
        return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    def _calculate_mob_curved_track_waypoints(
        self,
        track_points: list[tuple[float, float]],
        corridor_half_width_m: float,
        swath_m: float,
        altitude_m: float,
    ) -> list[tuple]:
        """
        Generate curved-track MOB search lanes that follow the ship's recorded path.

        Lanes expand outward from the track centre: on-track, +swath (stbd),
        -swath (port), +2*swath, -2*swath, …  Boustrophedon order connects the
        lanes into a single continuous snake.
        """
        if len(track_points) < 2 or swath_m <= 0 or corridor_half_width_m <= 0:
            return []

        # Remove consecutive GPS points that are closer than 12 m to suppress
        # noise-induced bearing spikes.
        MIN_SEP_M = 12.0
        filtered = [track_points[0]]
        for pt in track_points[1:]:
            la1, lo1 = filtered[-1]
            la2, lo2 = pt
            dlat = math.radians(la2 - la1)
            dlon = math.radians(lo2 - lo1)
            a = (
                math.sin(dlat / 2) ** 2
                + math.cos(math.radians(la1))
                * math.cos(math.radians(la2))
                * math.sin(dlon / 2) ** 2
            )
            if EARTH_RADIUS_METERS * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a))) >= MIN_SEP_M:
                filtered.append(pt)
        if len(filtered) < 2:
            filtered = list(track_points)
        track_points = filtered
        n = len(track_points)

        # Per-point bearings: average of in-bearing and out-bearing at interior points.
        raw_bearings: list[float] = []
        for i in range(n):
            if i == 0:
                b = self._bearing_between(*track_points[0], *track_points[1])
            elif i == n - 1:
                b = self._bearing_between(*track_points[-2], *track_points[-1])
            else:
                bi = self._bearing_between(*track_points[i - 1], *track_points[i])
                bo = self._bearing_between(*track_points[i], *track_points[i + 1])
                b = self._mean_bearing(bi, bo)
            raw_bearings.append(b)

        # Smooth with a 5-point circular-mean window to remove isolated spikes.
        HALF = 2
        local_bearings: list[float] = []
        for i in range(n):
            lo, hi = max(0, i - HALF), min(n, i + HALF + 1)
            xs = sum(math.cos(math.radians(b)) for b in raw_bearings[lo:hi])
            ys = sum(math.sin(math.radians(b)) for b in raw_bearings[lo:hi])
            local_bearings.append((math.degrees(math.atan2(ys, xs)) + 360.0) % 360.0)

        num_each_side = max(1, int(math.ceil(corridor_half_width_m / swath_m)))
        lane_offsets_m = [0.0]
        for i in range(1, num_each_side + 1):
            lane_offsets_m += [i * swath_m, -i * swath_m]

        waypoints: list[tuple] = []
        for lane_idx, offset_m in enumerate(lane_offsets_m):
            lane: list[tuple] = []
            for i, (lat, lon) in enumerate(track_points):
                if offset_m == 0.0:
                    lane.append((lat, lon, altitude_m))
                else:
                    perp = (local_bearings[i] + (90.0 if offset_m > 0 else 270.0)) % 360.0
                    p_lat, p_lon = self._offset_position(lat, lon, perp, abs(offset_m))
                    lane.append((p_lat, p_lon, altitude_m))
            if lane_idx % 2 != 0:
                lane = list(reversed(lane))
            waypoints.extend(lane)
        return waypoints

    def generate_and_upload_mob_search_mission(
        self,
        track_points: list[tuple[float, float]],
        corridor_half_width_m: float,
        swath_m: float,
        altitude_m: float,
        takeoff_altitude_m: float = 100.0,
        climb_speed_ms: float = 8.0,
        include_takeoff: bool = True,
    ) -> list[tuple] | None:
        """Generate and upload a curved-track MOB search mission.

        Returns a list of (lat, lon) path positions on success, or None on failure.
        """
        if not self.is_connected():
            return None

        wps = self._calculate_mob_curved_track_waypoints(
            track_points, corridor_half_width_m, swath_m, altitude_m
        )
        if not wps:
            self.logger.error("MAVLink: failed to generate MOB curved-track waypoints.")
            return None

        # Reverse the whole snake if the drone is closer to the far end.
        pos = self.get_current_position()
        if pos and len(wps) >= 2:
            def _dist_sq(pt: tuple) -> float:
                return (pos["lat"] - pt[0]) ** 2 + (pos["lon"] - pt[1]) ** 2

            if _dist_sq(wps[-1]) < _dist_sq(wps[0]):
                wps = list(reversed(wps))

        upload_wps: list = []
        if include_takeoff:
            tlat = pos["lat"] if pos else 0.0
            tlon = pos["lon"] if pos else 0.0
            upload_wps = [
                (tlat, tlon, 0.0, mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                 2.0, climb_speed_ms, -1.0, 0.0),
                (tlat, tlon, takeoff_altitude_m, mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                 0.0, 0.0, 0.0, float("nan")),
            ]

        path: list[tuple] = []
        for lat, lon, alt in wps:
            upload_wps.append(
                (lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                 0.0, 10.0, 0.0, float("nan"))
            )
            path.append((lat, lon))

        return path if self.upload_mission(upload_wps) else None
