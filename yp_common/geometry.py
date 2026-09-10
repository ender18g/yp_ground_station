"""Geographic transforms shared by vehicle bridges (metres and degrees)."""
import math

EARTH_RADIUS_M = 6_378_137.0


def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)
    bearing_rad = math.radians(bearing_deg)
    angular = distance_m / EARTH_RADIUS_M
    lat2 = math.asin(
        math.sin(lat_rad) * math.cos(angular)
        + math.cos(lat_rad) * math.sin(angular) * math.cos(bearing_rad)
    )
    lon2 = lon_rad + math.atan2(
        math.sin(bearing_rad) * math.sin(angular) * math.cos(lat_rad),
        math.cos(angular) - math.sin(lat_rad) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


def relative_waypoint_to_global(ship_lat: float, ship_lon: float, ship_heading: float, ship_alt: float, waypoint: dict) -> tuple[float, float, float]:
    local_x = float(waypoint.get("x", 0.0))
    local_y = float(waypoint.get("y", 0.0))
    local_z = float(waypoint.get("z", 0.0))
    distance_m = math.hypot(local_x, local_y)
    relative_bearing_deg = math.degrees(math.atan2(local_x, local_y))
    bearing_deg = (ship_heading + relative_bearing_deg + 360.0) % 360.0
    target_lat, target_lon = destination_point(ship_lat, ship_lon, bearing_deg, distance_m)
    return target_lat, target_lon, ship_alt + local_z


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def north_east_delta_m(lat_ref: float, lon_ref: float, lat: float, lon: float) -> tuple[float, float]:
    dlat = math.radians(lat - lat_ref)
    dlon = math.radians(lon - lon_ref)
    lat_avg = math.radians((lat_ref + lat) / 2.0)
    north_m = dlat * EARTH_RADIUS_M
    east_m = dlon * EARTH_RADIUS_M * math.cos(lat_avg)
    return north_m, east_m
