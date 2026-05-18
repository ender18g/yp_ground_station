import asyncio
import json
import time
import traceback

from pymavlink import mavutil
import websockets

VEHICLE_ID = "uav-mavlink-002"
WS_URL = f"ws://localhost:8000/ws/vehicle/{VEHICLE_ID}"
MAVLINK_URL = "udpin:127.0.0.1:14550"

# Waypoint offsets in degrees (approximately 100 meters at equator)
WAYPOINT_OFFSET = 0.001

def create_navsatfix_message(lat, lon, alt):
    now = time.time()
    sec = int(now)
    nanosec = int((now - sec) * 1e9)

    return {
        "vehicle_id": VEHICLE_ID,
        "vehicle_type": "uav",
        "topic": f"/vehicles/{VEHICLE_ID}/navsatfix",
        "type": "sensor_msgs/msg/NavSatFix",
        "stamp": now,
        "msg": {
            "header": {
                "stamp": {
                    "sec": sec,
                    "nanosec": nanosec
                },
                "frame_id": "map"
            },
            "status": {
                "status": 0,
                "service": 1
            },
            "latitude": lat,
            "longitude": lon,
            "altitude": alt,
            "position_covariance": [
                0, 0, 0,
                0, 0, 0,
                0, 0, 0
            ],
            "position_covariance_type": 0
        }
    }

def goto_waypoint(master, target_lat, target_lon, target_alt, timeout=30):
    """Send vehicle to a waypoint and wait for arrival"""
    print(f"\n[NAV] Flying to waypoint: {target_lat:.6f}, {target_lon:.6f}, alt={target_alt}m")
    
    master.mav.set_position_target_global_int_send(
        0,
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
        int(0b110111111000),
        int(target_lat * 1e7),
        int(target_lon * 1e7),
        target_alt,
        0, 0, 0,
        0, 0, 0,
        0, 0
    )
    
    # Wait for arrival (within 5 meters)
    start_time = time.time()
    arrival_threshold = 0.00005  # ~5 meters at equator
    
    while time.time() - start_time < timeout:
        msg = master.recv_match(
            type='GLOBAL_POSITION_INT',
            blocking=True,
            timeout=1
        )
        
        if msg is None:
            continue
            
        current_lat = msg.lat / 1e7
        current_lon = msg.lon / 1e7
        
        # Simple distance check
        lat_diff = abs(current_lat - target_lat)
        lon_diff = abs(current_lon - target_lon)
        
        if lat_diff < arrival_threshold and lon_diff < arrival_threshold:
            print(f"[NAV] Waypoint reached!")
            time.sleep(2)  # Hover briefly
            break

async def telemetry_stream_and_mission(master):
    """Stream telemetry to server and execute mission"""
    print(f"\n[INFO] Vehicle ID: {VEHICLE_ID}")
    print(f"[INFO] WebSocket URL: {WS_URL}")
    
    try:
        async with websockets.connect(WS_URL) as ws:
            print("[SUCCESS] WebSocket connected!")
            
            # Get takeoff position
            print("\n[NAV] Reading takeoff position...")
            msg = master.recv_match(
                type='GLOBAL_POSITION_INT',
                blocking=True,
                timeout=5
            )
            
            if msg is None:
                print("[ERROR] Could not read position")
                return
            
            takeoff_lat = msg.lat / 1e7
            takeoff_lon = msg.lon / 1e7
            takeoff_alt = msg.relative_alt / 1000.0
            
            print(f"[NAV] Takeoff position: {takeoff_lat:.6f}, {takeoff_lon:.6f}, alt={takeoff_alt:.2f}m")
            
            # Define three waypoints
            waypoints = [
                (takeoff_lat + WAYPOINT_OFFSET, takeoff_lon, 10),
                (takeoff_lat + WAYPOINT_OFFSET, takeoff_lon + WAYPOINT_OFFSET, 10),
                (takeoff_lat, takeoff_lon + WAYPOINT_OFFSET, 10),
            ]
            
            counter = 0
            mission_phase = "takeoff"
            current_waypoint = 0
            
            # Teleop loop - monitor position and stream telemetry
            while True:
                msg = master.recv_match(
                    type='GLOBAL_POSITION_INT',
                    blocking=True,
                    timeout=1
                )
                
                if msg is None:
                    continue
                
                counter += 1
                current_lat = msg.lat / 1e7
                current_lon = msg.lon / 1e7
                current_alt = msg.relative_alt / 1000.0
                
                # Print status periodically
                if counter % 10 == 0:
                    print(f"[POS] {current_lat:.6f}, {current_lon:.6f}, alt={current_alt:.2f}m")
                
                # Stream telemetry to server
                payload = create_navsatfix_message(current_lat, current_lon, current_alt)
                json_payload = json.dumps(payload)
                
                try:
                    await ws.send(json_payload)
                except Exception as e:
                    print(f"[ERROR] Failed to send telemetry: {e}")
                
                # Mission state machine
                if mission_phase == "takeoff":
                    if current_alt >= 9.5:  # Reached takeoff altitude
                        mission_phase = "waypoints"
                        current_waypoint = 0
                        print(f"\n[MISSION] Starting waypoint navigation")
                        target_lat, target_lon, target_alt = waypoints[current_waypoint]
                        goto_waypoint(master, target_lat, target_lon, target_alt)
                
                elif mission_phase == "waypoints":
                    target_lat, target_lon, target_alt = waypoints[current_waypoint]
                    lat_diff = abs(current_lat - target_lat)
                    lon_diff = abs(current_lon - target_lon)
                    
                    if lat_diff < 0.00005 and lon_diff < 0.00005:
                        current_waypoint += 1
                        if current_waypoint >= len(waypoints):
                            mission_phase = "return_to_launch"
                            print(f"\n[MISSION] All waypoints visited, returning to launch")
                            goto_waypoint(master, takeoff_lat, takeoff_lon, 10)
                        else:
                            print(f"\n[MISSION] Waypoint {current_waypoint} reached, heading to next")
                            target_lat, target_lon, target_alt = waypoints[current_waypoint]
                            goto_waypoint(master, target_lat, target_lon, target_alt)
                
                elif mission_phase == "return_to_launch":
                    lat_diff = abs(current_lat - takeoff_lat)
                    lon_diff = abs(current_lon - takeoff_lon)
                    
                    if lat_diff < 0.00005 and lon_diff < 0.00005:
                        mission_phase = "landing"
                        print(f"\n[MISSION] Reached home position, initiating landing")
                        
                        # Send land command
                        master.mav.command_long_send(
                            master.target_system,
                            master.target_component,
                            mavutil.mavlink.MAV_CMD_NAV_LAND,
                            0,
                            0, 0, 0, 0, 0, 0, 0
                        )
                        print("[MISSION] Land command sent")
                
                elif mission_phase == "landing":
                    if current_alt < 0.5:
                        print(f"\n[MISSION] Vehicle landed successfully!")
                        print("[INFO] Disarming...")
                        master.mav.command_long_send(
                            master.target_system,
                            master.target_component,
                            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                            0,
                            0, 0, 0, 0, 0, 0, 0
                        )
                        print("[SUCCESS] Mission complete!")
                        break
                
                await asyncio.sleep(0.1)
    
    except Exception as e:
        print(f"\n[ERROR] Connection failed!")
        print(str(e))
        print("\n[TRACEBACK]")
        traceback.print_exc()

async def main():
    print("\n==============================")
    print(" MAVLINK MISSION CONTROL")
    print("==============================\n")
    
    print(f"[INFO] MAVLink URL: {MAVLINK_URL}")
    
    # Connect to MAVLink
    print("[INFO] Connecting to MAVLink...")
    master = mavutil.mavlink_connection(MAVLINK_URL)
    
    print("[INFO] Waiting for heartbeat...")
    master.wait_heartbeat()
    
    print("[SUCCESS] Heartbeat received!")
    print(f"[INFO] Target System: {master.target_system}")
    print(f"[INFO] Target Component: {master.target_component}")
    
    # Request telemetry
    print("\n[INFO] Requesting telemetry stream...")
    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL,
        10,
        1
    )
    print("[SUCCESS] Telemetry stream requested")
    
    # Set GUIDED mode
    print("\n[INFO] Setting GUIDED mode...")
    mode = 'GUIDED'
    mode_id = master.mode_mapping()[mode]
    master.set_mode(mode_id)
    time.sleep(2)
    
    # Arm
    print("[INFO] Arming vehicle...")
    master.arducopter_arm()
    master.motors_armed_wait()
    print("[SUCCESS] Armed")
    
    # Takeoff
    print("\n[INFO] Taking off...")
    master.mav.command_long_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        0,
        0, 0, 0, 0, 0, 0,
        10
    )
    print("[INFO] Takeoff command sent")
    time.sleep(2)
    
    # Run mission with streaming
    await telemetry_stream_and_mission(master)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")