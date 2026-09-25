"""Mission math and waypoint generators.

Provides purely mathematical functions to calculate search-grid and MOB patterns.
Vehicle bridges call these generators to get raw (lat, lon, alt) tuples, then
handle the firmware-specific MAVLink execution themselves.
"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6_378_137.0


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
    min_leg_m       : minimum distance between consecutive waypoints within each lane.
    start_from_newest: if True the centre lane is traversed newest to oldest.
    """
    if len(track_points) < 2 or swath_m <= 0 or corridor_half_width_m <= 0:
        return []

    pts = [(float(p[0]), float(p[1])) for p in track_points]

    MIN_SEP_M = 12.0
    filtered = [pts[0]]
    for pt in pts[1:]:
        if _haversine_m(*filtered[-1], *pt) >= MIN_SEP_M:
            filtered.append(pt)
    if len(filtered) < 2:
        filtered = list(pts)

    decimated = [filtered[0]]
    for pt in filtered[1:]:
        if _haversine_m(*decimated[-1], *pt) >= min_leg_m:
            decimated.append(pt)
    if decimated[-1] != filtered[-1]:
        decimated.append(filtered[-1])
    pts = decimated if len(decimated) >= 2 else filtered
    n = len(pts)

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
        
        if (lane_idx % 2 != 0) ^ start_from_newest:
            lane = list(reversed(lane))
        waypoints.extend(lane)

    return waypoints