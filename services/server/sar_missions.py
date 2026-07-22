"""SAR mission math and MAVLink execution helpers.

Ported from lifeguard/components/mavlink_io.py and lifeguard/system/workers.py.
Used by vehicle bridges to execute search-grid and MOB missions without any
dependency on the full Lifeguard codebase.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable
from typing import Optional

from pymavlink import mavutil, mavwp

EARTH_RADIUS_M = 6_378_137.0
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Surface distance in metres between two lat/lon points."""
    la1, la2 = math.radians(lat1), math.radians(lat2)
    dlo = math.radians(lon2 - lon1)
    dlat = la2 - la1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2)
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _bearing_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth in degrees from (lat1,lon1) to (lat2,lon2)."""
    la1_r = math.radians(lat1)
    la2_r = math.radians(lat2)
    dlon_r = math.radians(lon2 - lon1)
    y = math.sin(dlon_r) * math.cos(la2_r)
    x = (math.cos(la1_r) * math.sin(la2_r)
         - math.sin(la1_r) * math.cos(la2_r) * math.cos(dlon_r))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _mean_bearing(b1: float, b2: float) -> float:
    """Circular mean of two bearings, handles 0/360 wrap."""
    x = math.cos(math.radians(b1)) + math.cos(math.radians(b2))
    y = math.sin(math.radians(b1)) + math.sin(math.radians(b2))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _offset_position(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Return (lat, lon) reached by travelling distance_m along bearing_deg."""
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    bearing_r = math.radians(bearing_deg)
    d = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat_r) * math.cos(d)
        + math.cos(lat_r) * math.sin(d) * math.cos(bearing_r)
    )
    lon2 = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(d) * math.cos(lat_r),
        math.cos(d) - math.sin(lat_r) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


# ---------------------------------------------------------------------------
# Waypoint generators
# ---------------------------------------------------------------------------

def calculate_search_grid_waypoints(
    center_lat: float,
    center_lon: float,
    grid_size_m: float,
    swath_m: float,
    altitude_m: float,
) -> list[tuple[float, float, float]]:
    """Return boustrophedon (lawnmower) waypoints as (lat, lon, alt) tuples."""
    if swath_m <= 0 or grid_size_m <= 0:
        return []
    clat_r = math.radians(center_lat)
    clon_r = math.radians(center_lon)
    half = grid_size_m / 2.0
    south_r = clat_r - half / EARTH_RADIUS_M
    north_r = clat_r + half / EARTH_RADIUS_M
    west_r = clon_r - half / (EARTH_RADIUS_M * math.cos(clat_r))
    step = swath_m / (EARTH_RADIUS_M * math.cos(clat_r))
    num_tracks = max(1, int(math.floor(grid_size_m / swath_m)))
    south_lat = math.degrees(south_r)
    north_lat = math.degrees(north_r)
    waypoints: list[tuple[float, float, float]] = []
    for i in range(num_tracks):
        lon_deg = math.degrees(west_r + i * step)
        if i % 2 == 0:
            waypoints.append((south_lat, lon_deg, altitude_m))
            waypoints.append((north_lat, lon_deg, altitude_m))
        else:
            waypoints.append((north_lat, lon_deg, altitude_m))
            waypoints.append((south_lat, lon_deg, altitude_m))
    return waypoints


def calculate_mob_waypoints(
    track_points: list,
    corridor_half_width_m: float,
    swath_m: float,
    altitude_m: float,
    min_leg_m: float = 30.0,
    start_from_newest: bool = False,
) -> list[tuple[float, float, float]]:
    """
    Return curved-track-following MOB search waypoints as (lat, lon, alt) tuples.

    track_points    : list of [lat, lon] pairs, oldest first.
    min_leg_m       : minimum distance between consecutive waypoints within each
                      lane.  Decimates the raw GPS track so the mission stays
                      manageable; 30 m default keeps waypoint counts low while
                      preserving enough curve fidelity.
    start_from_newest: if True the centre lane is traversed newest→oldest so the
                      vehicle begins near the most recent YP position.  Lane
                      ordering is always centre-outward regardless of this flag.
    """
    if len(track_points) < 2 or swath_m <= 0 or corridor_half_width_m <= 0:
        return []

    # Unpack whether each point is a tuple or a list
    pts = [(float(p[0]), float(p[1])) for p in track_points]

    # 1. Remove near-duplicate GPS samples caused by stationary periods / jitter
    MIN_SEP_M = 12.0
    filtered = [pts[0]]
    for pt in pts[1:]:
        if _haversine_m(*filtered[-1], *pt) >= MIN_SEP_M:
            filtered.append(pt)
    if len(filtered) < 2:
        filtered = list(pts)

    # 2. Decimate to min_leg_m spacing so each lane has a manageable waypoint count
    decimated = [filtered[0]]
    for pt in filtered[1:]:
        if _haversine_m(*decimated[-1], *pt) >= min_leg_m:
            decimated.append(pt)
    # Always keep the last (most-recent) fix so the search reaches the MOB site
    if decimated[-1] != filtered[-1]:
        decimated.append(filtered[-1])
    pts = decimated if len(decimated) >= 2 else filtered
    n = len(pts)

    # Per-point bearing: average of in/out bearings at interior points
    raw_bearings: list[float] = []
    for i in range(n):
        if i == 0:
            b = _bearing_between(*pts[0], *pts[1])
        elif i == n - 1:
            b = _bearing_between(*pts[-2], *pts[-1])
        else:
            b_in = _bearing_between(*pts[i - 1], *pts[i])
            b_out = _bearing_between(*pts[i], *pts[i + 1])
            b = _mean_bearing(b_in, b_out)
        raw_bearings.append(b)

    # Smooth bearings with a 5-point circular-mean window
    HALF = 2
    local_bearings: list[float] = []
    for i in range(n):
        lo = max(0, i - HALF)
        hi = min(n, i + HALF + 1)
        xs = sum(math.cos(math.radians(b)) for b in raw_bearings[lo:hi])
        ys = sum(math.sin(math.radians(b)) for b in raw_bearings[lo:hi])
        local_bearings.append((math.degrees(math.atan2(ys, xs)) + 360.0) % 360.0)

    num_each_side = max(1, int(math.ceil(corridor_half_width_m / swath_m)))
    lane_offsets_m = [0.0]
    for i in range(1, num_each_side + 1):
        lane_offsets_m.append(i * swath_m)    # starboard
        lane_offsets_m.append(-i * swath_m)   # port

    waypoints: list[tuple[float, float, float]] = []
    for lane_idx, offset_m in enumerate(lane_offsets_m):
        lane: list[tuple[float, float, float]] = []
        for i, (lat, lon) in enumerate(pts):
            if offset_m == 0.0:
                lane.append((lat, lon, altitude_m))
            else:
                perp = (local_bearings[i] + (90.0 if offset_m > 0 else 270.0)) % 360.0
                p_lat, p_lon = _offset_position(lat, lon, perp, abs(offset_m))
                lane.append((p_lat, p_lon, altitude_m))
        # Boustrophedon: alternate traversal direction per lane.
        # XOR with start_from_newest so that lane 0 (centre) always begins at
        # the requested track end, keeping lane ordering centre-outward.
        if (lane_idx % 2 != 0) ^ start_from_newest:
            lane = list(reversed(lane))
        waypoints.extend(lane)

    return waypoints


# ---------------------------------------------------------------------------
# MAVLink mission execution helpers
# ---------------------------------------------------------------------------

def upload_mission(master, waypoints_data: list) -> bool:
    """
    Upload a mission to the vehicle.

    waypoints_data: list of (lat, lon, alt, command_id, p1, p2, p3, p4) tuples.
    Returns True on success.
    """
    master.target_component = 1
    wp_loader = mavwp.MAVWPLoader()
    seq = 0

    # Home waypoint (seq 0) — set to first waypoint's lat/lon at ground level
    home_lat, home_lon = float(waypoints_data[0][0]), float(waypoints_data[0][1])
    wp_loader.add(
        mavutil.mavlink.MAVLink_mission_item_int_message(
            master.target_system, 1, seq,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
            mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
            0, 1, 0, 0, 0, 0,
            int(home_lat * 1e7), int(home_lon * 1e7), 0,
        )
    )
    seq += 1

    for item in waypoints_data:
        lat, lon, alt, command_id, p1, p2, p3, p4 = (
            float(item[0]), float(item[1]), float(item[2]),
            int(item[3]), float(item[4]), float(item[5]), float(item[6]), float(item[7]),
        )
        wp_loader.add(
            mavutil.mavlink.MAVLink_mission_item_int_message(
                master.target_system, 1, seq,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                command_id, 0, 1, p1, p2, p3, p4,
                int(lat * 1e7), int(lon * 1e7), alt,
            )
        )
        seq += 1

    if wp_loader.count() == 0:
        logger.error("No valid waypoints to upload")
        return False

    try:
        master.mav.mission_clear_all_send(master.target_system, 1)
        master.recv_match(type="MISSION_ACK", blocking=True, timeout=5)
        # Flush stale messages
        while master.recv_match(blocking=False):
            pass
        time.sleep(0.5)

        master.mav.mission_count_send(
            master.target_system, 1, wp_loader.count(),
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
        )
        for i in range(wp_loader.count()):
            msg = master.recv_match(
                type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
                blocking=True, timeout=10,
            )
            if not msg:
                logger.error(f"No MISSION_REQUEST for waypoint {i} (timeout)")
                return False
            if msg.get_type() == "MISSION_ACK":
                logger.error(f"Unexpected MISSION_ACK at waypoint {i}: vehicle rejected mission")
                return False
            master.mav.send(wp_loader.wp(msg.seq))
            if msg.seq == wp_loader.count() - 1:
                break

        final_ack = master.recv_match(type="MISSION_ACK", blocking=True, timeout=10)
        if final_ack and final_ack.type == mavutil.mavlink.MAV_MISSION_ACCEPTED:
            logger.info(f"Mission upload successful ({wp_loader.count()} items)")
            return True
        logger.error(f"Mission upload failed or bad final ACK: {final_ack}")
        return False
    except Exception as exc:
        logger.error(f"Mission upload error: {exc}")
        return False


def set_mode(
    master,
    mode_name: str,
    *,
    wait_for_ack: bool = True,
    ack_timeout_s: float = 5.0,
) -> bool:
    """Set vehicle flight mode.

    Latency-sensitive callers can skip the COMMAND_ACK wait and rely on MAVLink
    message ordering plus a brief settle delay.
    """
    mode_mapping = master.mode_mapping()
    if mode_mapping is None or mode_name.upper() not in mode_mapping:
        logger.error(f"Unknown mode: {mode_name}")
        return False
    mode_id = mode_mapping[mode_name.upper()]
    master.mav.set_mode_send(
        master.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id,
    )
    if not wait_for_ack:
        return True
    ack = wait_for_command_ack(master, mavutil.mavlink.MAV_CMD_DO_SET_MODE, ack_timeout_s)
    if ack and ack.command == mavutil.mavlink.MAV_CMD_DO_SET_MODE:
        return ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
    logger.warning(f"No ACK for mode {mode_name}, assuming success")
    return True


def wait_for_command_ack(master, command_id: int, timeout_s: float):
    """Wait for a specific COMMAND_ACK, ignoring stale ACKs for other commands."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=remaining)
        if not ack:
            return None
        if ack.command == command_id:
            return ack
        logger.debug(
            "Ignoring unrelated COMMAND_ACK while waiting for %s: command=%s result=%s",
            command_id,
            getattr(ack, "command", None),
            getattr(ack, "result", None),
        )


def arm_vehicle(master, force: bool = False) -> bool:
    """Arm the vehicle. force=True bypasses ArduPilot pre-arm checks (for emergency MOB)."""
    param2 = 21196.0 if force else 0.0
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        0, 1, param2, 0, 0, 0, 0, 0,
    )
    ack = wait_for_command_ack(master, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 5.0)
    if ack and ack.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
        if ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
            logger.info(f"Vehicle armed (force={force})")
            return True
    logger.error(f"Arming failed: {ack}")
    return False


def start_mission(
    master,
    retries: int = 2,
    retry_delay: float = 0.35,
    ack_timeout_s: float = 0.75,
) -> bool:
    """Send MISSION_START with retries. Returns True on success."""
    for attempt in range(1, retries + 1):
        master.mav.command_long_send(
            master.target_system, master.target_component,
            mavutil.mavlink.MAV_CMD_MISSION_START,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
        ack = wait_for_command_ack(master, mavutil.mavlink.MAV_CMD_MISSION_START, ack_timeout_s)
        if ack and ack.command == mavutil.mavlink.MAV_CMD_MISSION_START:
            if ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                logger.info("Mission start accepted")
                return True
        logger.warning(
            f"Mission start attempt {attempt}/{retries} failed: {ack}"
            + (f", retrying in {retry_delay}s" if attempt < retries else "")
        )
        if attempt < retries:
            time.sleep(retry_delay)
    logger.error("Mission start failed after all retries")
    return False


# ---------------------------------------------------------------------------
# High-level mission executors
# ---------------------------------------------------------------------------

def execute_search_grid(
    master,
    center_lat: float,
    center_lon: float,
    grid_size_m: float,
    swath_m: float,
    altitude_m: float,
    include_takeoff: bool = True,
    takeoff_altitude_m: float = 30.0,
    climb_speed_ms: float = 8.0,
) -> bool:
    """
    Generate, upload, arm, and start a boustrophedon search grid mission.
    Returns True on success.
    """
    waypoints = calculate_search_grid_waypoints(
        center_lat, center_lon, grid_size_m, swath_m, altitude_m
    )
    if not waypoints:
        logger.error("Failed to generate search grid waypoints")
        return False

    waypoints_data: list = []
    if include_takeoff:
        waypoints_data += [
            (
                center_lat, center_lon, 0.0,
                mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                2.0, climb_speed_ms, -1.0, 0.0,
            ),
            (
                center_lat, center_lon, takeoff_altitude_m,
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                0.0, 0.0, 0.0, float("nan"),
            ),
        ]
        logger.info(f"Search grid takeoff: {takeoff_altitude_m}m at {climb_speed_ms}m/s")

    for lat, lon, alt in waypoints:
        waypoints_data.append(
            (lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0.0, 10.0, 0.0, float("nan"))
        )

    if not upload_mission(master, waypoints_data):
        logger.error("Search grid mission upload failed")
        return False

    set_mode(master, "GUIDED")
    time.sleep(1.5)

    if not arm_vehicle(master, force=include_takeoff):
        logger.error("Arming failed for search grid")
        return False
    time.sleep(0.5)

    set_mode(master, "AUTO")
    time.sleep(1.0)

    if not start_mission(master):
        logger.error("Mission start failed for search grid")
        return False

    logger.info(
        f"Search grid mission running: center=({center_lat:.6f},{center_lon:.6f}), "
        f"{grid_size_m}m grid, {swath_m}m swath, {altitude_m}m alt, {len(waypoints)} waypoints"
    )
    return True


def execute_mob_search(
    master,
    track_points: list,
    corridor_half_width_m: float = 50.0,
    swath_m: float = 20.0,
    altitude_m: float = 30.0,
    takeoff_altitude_m: float = 30.0,
    climb_speed_ms: float = 9.0,
    include_takeoff: bool = True,
) -> bool:
    """
    Generate, upload, arm, and start a curved-track-following MOB search mission.
    Returns True on success.
    """
    # Choose traversal direction: start from whichever track end is closest to the
    # vehicle so it reaches the search area as quickly as possible.  Lane ordering
    # is always centre-outward regardless.
    start_from_newest = False
    try:
        msg = master.messages.get("GLOBAL_POSITION_INT") if hasattr(master, "messages") else None
        if msg and len(track_points) >= 2:
            v_lat, v_lon = msg.lat / 1e7, msg.lon / 1e7
            dist_oldest = _haversine_m(v_lat, v_lon, float(track_points[0][0]), float(track_points[0][1]))
            dist_newest = _haversine_m(v_lat, v_lon, float(track_points[-1][0]), float(track_points[-1][1]))
            start_from_newest = dist_newest < dist_oldest
    except Exception as exc:
        logger.warning(f"Could not orient MOB pattern from current position: {exc}")

    waypoints = calculate_mob_waypoints(
        track_points, corridor_half_width_m, swath_m, altitude_m,
        start_from_newest=start_from_newest,
    )
    if not waypoints:
        logger.error("Failed to generate MOB waypoints")
        return False

    # Speed command is always the first mission item (covers both UAV and surface vehicles)
    start_lat, start_lon = waypoints[0][0], waypoints[0][1]
    waypoints_data: list = [
        (
            start_lat, start_lon, 0.0,
            mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
            2.0, climb_speed_ms, -1.0, 0.0,
        ),
    ]
    if include_takeoff:
        waypoints_data.append((
            start_lat, start_lon, takeoff_altitude_m,
            mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
            0.0, 0.0, 0.0, float("nan"),
        ))
        logger.info(f"MOB takeoff: {takeoff_altitude_m}m at {climb_speed_ms}m/s")

    for lat, lon, alt in waypoints:
        waypoints_data.append(
            (lat, lon, alt, mavutil.mavlink.MAV_CMD_NAV_WAYPOINT, 0.0, 10.0, 0.0, float("nan"))
        )

    if not upload_mission(master, waypoints_data):
        logger.error("MOB mission upload failed")
        return False

    set_mode(master, "GUIDED")
    time.sleep(1.5)

    if not arm_vehicle(master, force=True):   # Force arm for emergency MOB
        logger.error("Force arming failed for MOB")
        return False
    time.sleep(0.5)

    set_mode(master, "AUTO")
    time.sleep(1.0)

    if not start_mission(master):
        logger.error("Mission start failed for MOB")
        return False

    logger.info(
        f"MOB search mission running: {len(track_points)} track points \u2192 {len(waypoints)} waypoints, "
        f"{corridor_half_width_m}m half-width, {swath_m}m swath, {altitude_m}m alt"
    )
    return True


# ---------------------------------------------------------------------------
# Streaming / carrot-chasing mission executors
# ---------------------------------------------------------------------------

def stream_waypoints_guided(
    master,
    waypoints: list[tuple[float, float, float]],
    include_takeoff: bool = False,
    takeoff_altitude_m: float = 30.0,
    climb_speed_ms: float = 8.0,
    force_arm: bool = False,
    arrival_radius_m: float = 10.0,
    wp_resend_interval_s: float = 2.0,
    stop_event: Optional = None,
    telemetry_callback: Optional[Callable[[object], None]] = None,
) -> bool:
    """
    Stream waypoints one at a time in GUIDED mode (chasing-the-carrot pattern).

    Instead of uploading the full mission at once (which can fail on lossy radio
    links), this function sends a single SET_POSITION_TARGET_GLOBAL_INT command
    per waypoint and only advances to the next once the vehicle is within
    arrival_radius_m.  The target is periodically resent to handle dropped UDP
    packets.

    For UAVs (include_takeoff=True):
        Uploads a minimal 2-item takeoff mission first (far less likely to fail
        than a full grid upload), waits until the vehicle reaches cruise
        altitude, then switches to GUIDED and streams the waypoints.

    For surface vehicles (include_takeoff=False):
        Arms in GUIDED mode and streams waypoints directly — no mission upload
        is needed at all.
    """
    if not waypoints:
        logger.error("stream_waypoints_guided: no waypoints provided")
        return False

    is_surface = not include_takeoff

    def _send_wp(lat: float, lon: float, alt: float) -> None:
        master.mav.set_position_target_global_int_send(
            0,
            master.target_system, master.target_component,
            mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            int(0b110111111000),  # use position only (ignore velocity/accel)
            int(lat * 1e7), int(lon * 1e7),
            0.0 if is_surface else alt,
            0.0, 0.0, 0.0,
            0.0, 0.0, 0.0,
            0.0, 0.0,
        )

    def _get_pos():
        """Poll for GLOBAL_POSITION_INT, return (lat, lon, rel_alt_m) or Nones."""
        for _ in range(5):
            msg = master.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=0.5)
            if msg:
                if telemetry_callback is not None:
                    try:
                        telemetry_callback(msg)
                    except Exception as exc:
                        logger.debug(f"Streaming telemetry callback error: {exc}")
                return msg.lat / 1e7, msg.lon / 1e7, msg.relative_alt / 1000.0
        return None, None, None

    # ---- Initial setup ----
    if include_takeoff and not is_surface:
        # Upload a minimal 2-waypoint takeoff-only mission.  Much smaller than a
        # full grid upload so far less likely to fail over a lossy radio link.
        start_lat, start_lon = waypoints[0][0], waypoints[0][1]
        takeoff_data = [
            (
                start_lat, start_lon, 0.0,
                mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                2.0, climb_speed_ms, -1.0, 0.0,
            ),
            (
                start_lat, start_lon, takeoff_altitude_m,
                mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                0.0, 0.0, 0.0, float("nan"),
            ),
        ]
        if not upload_mission(master, takeoff_data):
            logger.error("Streaming: minimal takeoff mission upload failed")
            return False

        set_mode(master, "GUIDED", wait_for_ack=False)
        time.sleep(0.2)
        if not arm_vehicle(master, force=force_arm):
            logger.error("Streaming: arming failed")
            return False
        time.sleep(0.2)
        set_mode(master, "AUTO", wait_for_ack=False)
        time.sleep(0.2)
        if not start_mission(master):
            logger.error("Streaming: mission start failed")
            return False

        logger.info(f"Streaming: waiting for takeoff to {takeoff_altitude_m:.0f} m")
        takeoff_deadline = time.monotonic() + 180.0
        while time.monotonic() < takeoff_deadline:
            if stop_event and stop_event.is_set():
                logger.info("Streaming: cancelled during takeoff")
                return False
            _, _, cur_alt = _get_pos()
            if cur_alt is not None and cur_alt >= takeoff_altitude_m * 0.85:
                logger.info(f"Streaming: cruise altitude reached ({cur_alt:.1f} m), switching to GUIDED")
                break
        else:
            logger.error("Streaming: takeoff timed out after 3 min")
            return False

        set_mode(master, "GUIDED", wait_for_ack=False)
        time.sleep(0.2)

    else:
        # Surface vehicle / no-takeoff: set GUIDED mode, arm, then set cruise speed
        set_mode(master, "GUIDED", wait_for_ack=False)
        time.sleep(0.2)
        if not arm_vehicle(master, force=force_arm):
            logger.error("Streaming: arming failed")
            return False
        time.sleep(0.2)
        # Surface vehicles ignore mission DO_CHANGE_SPEED; send it as a command_long
        if climb_speed_ms > 0:
            master.mav.command_long_send(
                master.target_system, master.target_component,
                mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                0,               # confirmation
                1,               # param1: 1 = groundspeed
                climb_speed_ms,  # param2: speed in m/s
                -1,              # param3: no throttle change
                0, 0, 0, 0,
            )
            time.sleep(0.2)

    # ---- Carrot-chase waypoint loop ----
    logger.info(f"Streaming: chasing {len(waypoints)} waypoints in GUIDED mode")
    for i, (lat, lon, alt) in enumerate(waypoints):
        if stop_event and stop_event.is_set():
            logger.info(f"Streaming: cancelled at waypoint {i + 1}/{len(waypoints)}")
            return False

        _send_wp(lat, lon, alt)
        logger.info(
            f"Streaming: waypoint {i + 1}/{len(waypoints)}"
            f" ({lat:.6f}, {lon:.6f}, alt={0.0 if is_surface else alt:.0f}m)"
        )
        last_send = time.monotonic()
        wp_deadline = time.monotonic() + 600.0  # 10 min per-waypoint timeout

        while time.monotonic() < wp_deadline:
            if stop_event and stop_event.is_set():
                logger.info(f"Streaming: cancelled at waypoint {i + 1}/{len(waypoints)}")
                return False

            cur_lat, cur_lon, cur_alt = _get_pos()
            if cur_lat is not None:
                dist = _haversine_m(cur_lat, cur_lon, lat, lon)
                if is_surface or cur_alt is None:
                    arrived = dist <= arrival_radius_m
                else:
                    alt_err = abs(cur_alt - alt)
                    arrived = dist <= arrival_radius_m and alt_err <= max(3.0, arrival_radius_m * 0.5)
                if arrived:
                    logger.info(
                        f"Streaming: reached waypoint {i + 1}/{len(waypoints)}"
                        f" (dist={dist:.1f}m)"
                    )
                    break

            # Resend periodically to recover from dropped UDP packets
            if time.monotonic() - last_send >= wp_resend_interval_s:
                _send_wp(lat, lon, alt)
                last_send = time.monotonic()
        else:
            logger.warning(
                f"Streaming: waypoint {i + 1}/{len(waypoints)} timed out after 10 min, advancing"
            )

    logger.info("Streaming: all waypoints complete")
    return True


def execute_search_grid_streaming(
    master,
    center_lat: float,
    center_lon: float,
    grid_size_m: float,
    swath_m: float,
    altitude_m: float,
    include_takeoff: bool = True,
    takeoff_altitude_m: float = 30.0,
    climb_speed_ms: float = 8.0,
    arrival_radius_m: float = 10.0,
    stop_event: Optional = None,
    telemetry_callback: Optional[Callable[[object], None]] = None,
) -> bool:
    """
    Streaming version of execute_search_grid.  Generates the lawnmower pattern
    then calls stream_waypoints_guided instead of uploading the full mission.
    """
    waypoints = calculate_search_grid_waypoints(center_lat, center_lon, grid_size_m, swath_m, altitude_m)
    if not waypoints:
        logger.error("Streaming: failed to generate search grid waypoints")
        return False

    logger.info(
        f"Streaming search grid: {len(waypoints)} waypoints, "
        f"{grid_size_m}m grid, {swath_m}m swath, {altitude_m}m alt"
    )
    return stream_waypoints_guided(
        master, waypoints,
        include_takeoff=include_takeoff,
        takeoff_altitude_m=takeoff_altitude_m,
        climb_speed_ms=climb_speed_ms,
        force_arm=include_takeoff,
        arrival_radius_m=arrival_radius_m,
        stop_event=stop_event,
        telemetry_callback=telemetry_callback,
    )


def execute_mob_search_streaming(
    master,
    track_points: list,
    corridor_half_width_m: float = 50.0,
    swath_m: float = 20.0,
    altitude_m: float = 30.0,
    takeoff_altitude_m: float = 30.0,
    climb_speed_ms: float = 9.0,
    include_takeoff: bool = True,
    arrival_radius_m: float = 10.0,
    stop_event: Optional = None,
    telemetry_callback: Optional[Callable[[object], None]] = None,
) -> bool:
    """
    Streaming version of execute_mob_search.  Generates MOB waypoints then
    calls stream_waypoints_guided instead of uploading the full mission.
    """
    # Determine traversal direction from vehicle position vs track endpoints
    start_from_newest = False
    try:
        msg = master.messages.get("GLOBAL_POSITION_INT") if hasattr(master, "messages") else None
        if msg and len(track_points) >= 2:
            v_lat, v_lon = msg.lat / 1e7, msg.lon / 1e7
            dist_oldest = _haversine_m(v_lat, v_lon, float(track_points[0][0]), float(track_points[0][1]))
            dist_newest = _haversine_m(v_lat, v_lon, float(track_points[-1][0]), float(track_points[-1][1]))
            start_from_newest = dist_newest < dist_oldest
    except Exception as exc:
        logger.warning(f"Streaming: could not orient MOB pattern: {exc}")

    waypoints = calculate_mob_waypoints(
        track_points, corridor_half_width_m, swath_m, altitude_m,
        start_from_newest=start_from_newest,
    )
    if not waypoints:
        logger.error("Streaming: failed to generate MOB waypoints")
        return False

    logger.info(
        f"Streaming MOB search: {len(track_points)} track points \u2192 {len(waypoints)} waypoints, "
        f"{corridor_half_width_m}m half-width, {swath_m}m swath"
    )
    return stream_waypoints_guided(
        master, waypoints,
        include_takeoff=include_takeoff,
        takeoff_altitude_m=takeoff_altitude_m,
        climb_speed_ms=climb_speed_ms,
        force_arm=True,  # MOB always force-arms (emergency scenario)
        arrival_radius_m=arrival_radius_m,
        stop_event=stop_event,
        telemetry_callback=telemetry_callback,
    )
