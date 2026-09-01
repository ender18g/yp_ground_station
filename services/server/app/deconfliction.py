"""Vehicle-to-vehicle deconfliction module.

Handles collision detection and avoidance for multi-vehicle operations,
including mission priority hierarchy and course correction strategies.
"""
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple, Dict, Any

# Mission priority levels (higher number = higher priority)
MISSION_PRIORITY = {
    "mob": 4,           # Man Overboard (highest)
    "search_grid": 3,   # Search Grid pattern
    "mission_plan": 2,  # Mission Planner mission
    "waypoint": 1,      # Simple waypoint command
    "rtb": 1,           # Return to base (same as waypoint)
}

# Default deconfliction radius per vehicle type (meters)
DEFAULT_DECONFLICT_RADIUS_M = {
    "uav": 10.0,    # Small fixed-wing or quadrotor
    "uavf": 10.0,   # Fixed-wing
    "usv": 15.0,    # Surface vessel (larger)
    "ugv": 15.0,    # Ground vehicle
    "uuv": 15.0,    # Underwater vehicle
    "yp": 20.0,     # Mother vessel (largest)
}

EARTH_RADIUS_M = 6_378_137.0


@dataclass
class VehicleState:
    """Track mission and deconfliction state for a vehicle."""
    vehicle_id: str
    vehicle_type: str
    position: Optional[Dict[str, float]] = None  # lat, lon, alt, heading
    mission_type: Optional[str] = None           # current mission (mob, search_grid, etc.)
    mission_data: Dict[str, Any] = field(default_factory=dict)  # saved mission params
    is_paused: bool = False
    paused_reason: Optional[str] = None
    saved_command: Optional[Dict[str, Any]] = None  # saved command for resume
    avoidance_active: bool = False


class DeconflictionEngine:
    """Detects and resolves vehicle conflicts using mission priority hierarchy."""
    
    def __init__(self, enabled: bool = False, 
                 global_radius_m: float = 10.0,
                 radius_per_type: Optional[Dict[str, float]] = None):
        """Initialize deconfliction engine.
        
        Args:
            enabled: Enable/disable deconfliction
            global_radius_m: Default safety radius in meters
            radius_per_type: Override radius per vehicle type
        """
        self.enabled = enabled
        self.global_radius_m = global_radius_m
        self.radius_per_type = radius_per_type or DEFAULT_DECONFLICT_RADIUS_M.copy()
        self.vehicle_states: Dict[str, VehicleState] = {}
    
    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable deconfliction."""
        self.enabled = enabled
    
    def set_radius(self, vehicle_type: str, radius_m: float) -> None:
        """Set deconfliction radius for a vehicle type."""
        if radius_m > 0:
            self.radius_per_type[vehicle_type] = radius_m
    
    def get_radius(self, vehicle_type: str) -> float:
        """Get deconfliction radius for a vehicle type."""
        return self.radius_per_type.get(vehicle_type, self.global_radius_m)
    
    def update_vehicle(self, vehicle_id: str, vehicle_type: str, 
                      position: Optional[Dict[str, float]],
                      mission_type: Optional[str] = None) -> None:
        """Update vehicle position and mission state.
        
        Args:
            vehicle_id: Vehicle identifier
            vehicle_type: Vehicle type (uav, usv, etc.)
            position: Dict with latitude, longitude, altitude, optional heading
            mission_type: Current mission type (mob, search_grid, waypoint, etc.)
        """
        if vehicle_id not in self.vehicle_states:
            self.vehicle_states[vehicle_id] = VehicleState(
                vehicle_id=vehicle_id,
                vehicle_type=vehicle_type,
            )
        
        state = self.vehicle_states[vehicle_id]
        state.position = position
        if mission_type:
            state.mission_type = mission_type
    
    def set_mission_data(self, vehicle_id: str, mission_data: Dict[str, Any]) -> None:
        """Save mission parameters for later resume."""
        if vehicle_id in self.vehicle_states:
            self.vehicle_states[vehicle_id].mission_data = mission_data.copy()
    
    def set_saved_command(self, vehicle_id: str, command: Dict[str, Any]) -> None:
        """Save the command for resume after deconfliction."""
        if vehicle_id in self.vehicle_states:
            self.vehicle_states[vehicle_id].saved_command = command.copy()

    def begin_avoidance(self, vehicle_id: str) -> bool:
        """Mark a vehicle as temporarily diverted, returning whether it was newly marked."""
        state = self.vehicle_states.get(vehicle_id)
        if not state or state.avoidance_active:
            return False
        state.avoidance_active = True
        return True

    def end_avoidance(self, vehicle_id: str) -> Optional[Dict[str, Any]]:
        """Clear a temporary diversion and return the command to resume."""
        state = self.vehicle_states.get(vehicle_id)
        if not state or not state.avoidance_active:
            return None
        state.avoidance_active = False
        return state.saved_command.copy() if state.saved_command else None

    def active_avoidance_vehicle_ids(self) -> set[str]:
        """Return vehicles currently executing a temporary avoidance waypoint."""
        return {
            vehicle_id
            for vehicle_id, state in self.vehicle_states.items()
            if state.avoidance_active
        }
    
    def detect_conflicts(self) -> list[Tuple[str, str]]:
        """Detect all vehicle conflicts.
        
        Returns:
            List of (vehicle_id1, vehicle_id2) tuples in conflict.
            Only returns each pair once, with lower-priority vehicle first.
        """
        if not self.enabled:
            return []
        
        conflicts = []
        vehicle_ids = list(self.vehicle_states.keys())
        
        for i, vid1 in enumerate(vehicle_ids):
            state1 = self.vehicle_states[vid1]
            if not state1.position:
                continue
            
            for vid2 in vehicle_ids[i+1:]:
                state2 = self.vehicle_states[vid2]
                if not state2.position:
                    continue
                
                distance_m = self._calculate_distance_3d(state1.position, state2.position)
                radius1 = self.get_radius(state1.vehicle_type)
                radius2 = self.get_radius(state2.vehicle_type)
                min_distance = radius1 + radius2
                
                if distance_m < min_distance:
                    # Return with lower-priority vehicle first
                    priority1 = MISSION_PRIORITY.get(state1.mission_type, 0)
                    priority2 = MISSION_PRIORITY.get(state2.mission_type, 0)
                    if priority1 <= priority2:
                        conflicts.append((vid1, vid2))
                    else:
                        conflicts.append((vid2, vid1))
        
        return conflicts
    
    def calculate_deconfliction_waypoint(self, 
                                        low_priority_id: str,
                                        high_priority_id: str,
                                        orbit_radius_m: float = 50.0) -> Optional[Tuple[float, float, float]]:
        """Calculate a waypoint to steer low-priority vehicle around high-priority one.
        
        Args:
            low_priority_id: Vehicle to be steered (lower priority)
            high_priority_id: Vehicle to avoid (higher priority)
            orbit_radius_m: Radius to orbit around conflict point
        
        Returns:
            (latitude, longitude, altitude) tuple for the deconfliction waypoint,
            or None if calculation fails.
        """
        state_low = self.vehicle_states.get(low_priority_id)
        state_high = self.vehicle_states.get(high_priority_id)
        
        if not state_low or not state_low.position or not state_high or not state_high.position:
            return None
        
        pos_low = state_low.position
        pos_high = state_high.position
        
        # Get bearing from high to low priority vehicle
        bearing = self._calculate_bearing(
            pos_high.get("latitude", 0),
            pos_high.get("longitude", 0),
            pos_low.get("latitude", 0),
            pos_low.get("longitude", 0),
        )
        
        # Calculate offset waypoint in direction away from high-priority vehicle
        offset_waypoint = self._offset_by_bearing_and_distance(
            pos_low.get("latitude", 0),
            pos_low.get("longitude", 0),
            bearing,
            orbit_radius_m,
        )
        
        # Use low-priority vehicle's current altitude
        altitude = pos_low.get("altitude", 0)
        
        return (offset_waypoint[0], offset_waypoint[1], altitude)
    
    def pause_vehicle(self, vehicle_id: str, reason: str) -> None:
        """Mark a vehicle as paused (stopped for deconfliction)."""
        if vehicle_id in self.vehicle_states:
            self.vehicle_states[vehicle_id].is_paused = True
            self.vehicle_states[vehicle_id].paused_reason = reason
    
    def resume_vehicle(self, vehicle_id: str) -> Optional[Dict[str, Any]]:
        """Resume a paused vehicle, returning the saved command."""
        if vehicle_id in self.vehicle_states:
            state = self.vehicle_states[vehicle_id]
            state.is_paused = False
            state.paused_reason = None
            return state.saved_command
        return None
    
    def is_vehicle_paused(self, vehicle_id: str) -> bool:
        """Check if vehicle is paused."""
        return self.vehicle_states.get(vehicle_id, VehicleState(
            vehicle_id="", vehicle_type=""
        )).is_paused
    
    def clear_vehicle_state(self, vehicle_id: str) -> None:
        """Remove a vehicle from tracking (e.g., when it disconnects)."""
        self.vehicle_states.pop(vehicle_id, None)
    
    # ========== Private utility methods ==========
    
    @staticmethod
    def _calculate_distance_3d(pos1: Dict[str, float], 
                               pos2: Dict[str, float]) -> float:
        """Calculate 3D Euclidean distance between two positions (meters).
        
        Uses great-circle distance for lat/lon, then Pythagorean for altitude.
        """
        lat1 = pos1.get("latitude", 0)
        lon1 = pos1.get("longitude", 0)
        alt1 = pos1.get("altitude", 0)
        
        lat2 = pos2.get("latitude", 0)
        lon2 = pos2.get("longitude", 0)
        alt2 = pos2.get("altitude", 0)
        
        # Haversine distance (horizontal)
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        horizontal_m = 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
        
        # Altitude difference
        dalt = alt2 - alt1
        
        # 3D distance
        distance_3d = math.sqrt(horizontal_m ** 2 + dalt ** 2)
        return distance_3d
    
    @staticmethod
    def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2 (degrees, 0-360)."""
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        
        y = math.sin(dlon) * math.cos(math.radians(lat2))
        x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
            math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(dlon)
        
        bearing = math.atan2(y, x)
        bearing_deg = math.degrees(bearing)
        return (bearing_deg + 360) % 360
    
    @staticmethod
    def _offset_by_bearing_and_distance(lat: float, lon: float, 
                                       bearing_deg: float, distance_m: float) -> Tuple[float, float]:
        """Calculate new position offset by bearing and distance.
        
        Args:
            lat, lon: Starting latitude/longitude
            bearing_deg: Bearing in degrees (0-360)
            distance_m: Distance in meters
        
        Returns:
            (new_lat, new_lon) tuple
        """
        lat_r = math.radians(lat)
        lon_r = math.radians(lon)
        bearing_r = math.radians(bearing_deg)
        d = distance_m / EARTH_RADIUS_M
        
        lat2 = math.asin(
            math.sin(lat_r) * math.cos(d) +
            math.cos(lat_r) * math.sin(d) * math.cos(bearing_r)
        )
        lon2 = lon_r + math.atan2(
            math.sin(bearing_r) * math.sin(d) * math.cos(lat_r),
            math.cos(d) - math.sin(lat_r) * math.sin(lat2),
        )
        
        return math.degrees(lat2), math.degrees(lon2)
