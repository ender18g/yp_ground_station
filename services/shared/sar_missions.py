"""SAR mission math and MAVLink execution helpers.

Ported from lifeguard/components/mavlink_io.py and lifeguard/system/workers.py.
Used by vehicle bridges to execute search-grid and MOB missions without any
dependency on the full Lifeguard codebase.

Canonical location: services/shared/sar_missions.py
Both services/server and services/arducopter_ws_bridge copy this file into
their Docker images at build time via their respective Dockerfiles.
Do NOT edit the per-service copies — edit this file only.
"""
from __future__ import annotations

import logging
import math
import time
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
) -> list[tuple[float, float, float]]:
    """
    Return curved-track-following MOB search waypoints as (lat, lon, alt) tuples.

    track_points: list of [lat, lon] pairs (JSON arrays or tuples), oldest first.
    Lanes expand outward from the ship track: on-track, +swath stbd, -swath port, etc.
    Boustrophedon order: even lanes oldest→newest, odd lanes newest→oldest.
    """
    if len(track_points) < 2 or swath_m <= 0 or corridor_half_width_m <= 0:
        return []

    # Unpack whether each point is a tuple or a list
    pts = [(float(p[0]), float(p[1])) for p in track_points]

    # Discard consecutive near-duplicate GPS samples (12 m threshold)
    MIN_SEP_M = 12.0
    filtered = [pts[0]]
    for pt in pts[1:]:
        if _haversine_m(*filtered[-1], *pt) >= MIN_SEP_M:
            filtered.append(pt)
    if len(filtered) < 2:
        filtered = list(pts)
    pts = filtered
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
        if lane_idx % 2 != 0:
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


def set_mode(master, mode_name: str) -> bool:
    """Set vehicle flight mode. Returns True on success (or assumed success on ACK timeout)."""
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
    ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    if ack and ack.command == mavutil.mavlink.MAV_CMD_DO_SET_MODE:
        return ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
    logger.warning(f"No ACK for mode {mode_name}, assuming success")
    return True


def arm_vehicle(master, force: bool = False) -> bool:
    """Arm the vehicle. force=True bypasses ArduPilot pre-arm checks (for emergency MOB)."""
    param2 = 21196.0 if force else 0.0
    master.mav.command_long_send(
        master.target_system, master.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        0, 1, param2, 0, 0, 0, 0, 0,
    )
    ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)
    if ack and ack.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM:
        if ack.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
            logger.info(f"Vehicle armed (force={force})")
            return True
    logger.error(f"Arming failed: {ack}")
    return False


def start_mission(master, retries: int = 3, retry_delay: float = 1.5) -> bool:
    """Send MISSION_START with retries. Returns True on success."""
    for attempt in range(1, retries + 1):
        master.mav.command_long_send(
            master.target_system, master.target_component,
            mavutil.mavlink.MAV_CMD_MISSION_START,
            0, 0, 0, 0, 0, 0, 0, 0,
        )
        ack = master.recv_match(type="COMMAND_ACK", blocking=True, timeout=3)
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
    climb_speed_ms: float = 8.0,
    include_takeoff: bool = True,
) -> bool:
    """
    Generate, upload, arm, and start a curved-track-following MOB search mission.
    Returns True on success.
    """
    waypoints = calculate_mob_waypoints(
        track_points, corridor_half_width_m, swath_m, altitude_m
    )
    if not waypoints:
        logger.error("Failed to generate MOB waypoints")
        return False

    # Orient pattern so vehicle starts from the end closest to its current position
    try:
        msg = master.messages.get("GLOBAL_POSITION_INT") if hasattr(master, "messages") else None
        if msg:
            d_lat, d_lon = msg.lat / 1e7, msg.lon / 1e7
            dist_first = _haversine_m(d_lat, d_lon, waypoints[0][0], waypoints[0][1])
            dist_last = _haversine_m(d_lat, d_lon, waypoints[-1][0], waypoints[-1][1])
            if dist_last < dist_first:
                waypoints = list(reversed(waypoints))
    except Exception as exc:
        logger.warning(f"Could not orient MOB pattern from current position: {exc}")

    waypoints_data: list = []
    if include_takeoff:
        start_lat, start_lon = waypoints[0][0], waypoints[0][1]
        waypoints_data += [
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
        f"MOB search mission running: {len(track_points)} track points, "
        f"{corridor_half_width_m}m half-width, {swath_m}m swath, {len(waypoints)} waypoints"
    )
    return True
