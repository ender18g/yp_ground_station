import asyncio
import json
import time
import traceback

from pymavlink import mavutil
import websockets

VEHICLE_ID = "uav-mavlink-001"

WS_URL = f"ws://localhost:8000/ws/vehicle/{VEHICLE_ID}"

MAVLINK_URL = "udpin:127.0.0.1:14550"


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



async def telemetry_loop():

    print("\n==============================")
    print(" MAVLINK → WEBSOCKET BRIDGE ")
    print("==============================\n")

    print(f"[INFO] Vehicle ID: {VEHICLE_ID}")
    print(f"[INFO] MAVLink URL: {MAVLINK_URL}")
    print(f"[INFO] WebSocket URL: {WS_URL}")

    #
    # CONNECT TO MAVLINK
    #

    print("\n[INFO] Connecting to MAVLink...")

    master = mavutil.mavlink_connection(MAVLINK_URL)


    print("[INFO] Waiting for heartbeat...")

    master.wait_heartbeat()

    print("[SUCCESS] Heartbeat received!")
    print(f"[INFO] Target System: {master.target_system}")
    print(f"[INFO] Target Component: {master.target_component}")

    #
    # REQUEST POSITION STREAM
    #

    print("\n[INFO] Requesting position telemetry stream...")

    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        10,   # Hz
        1
    )

    print("[SUCCESS] Telemetry stream requested")

    #
    # CONNECT WEBSOCKET
    #

    print(f"\n[INFO] Connecting websocket to:")
    print(f"       {WS_URL}")

    try:

        async with websockets.connect(WS_URL) as ws:

            print("[SUCCESS] WebSocket connected!")

            #
            # MAIN LOOP
            #

            counter = 0

            while True:

                #
                # RECEIVE MAVLINK MESSAGE
                #

                msg = master.recv_match(
                    type="GLOBAL_POSITION_INT",
                    blocking=True,
                    timeout=5
                )

                if msg is None:
                    print("[WARNING] No GLOBAL_POSITION_INT received")
                    continue

                counter += 1

                print(f"\n--- MAVLINK MESSAGE #{counter} ---")

                print("[RAW]")
                print(msg)

                #
                # CONVERT MAVLINK DATA
                #

                lat = msg.lat / 1e7
                lon = msg.lon / 1e7
                alt = msg.relative_alt / 1000.0

                print("[PARSED]")
                print(f"Latitude : {lat}")
                print(f"Longitude: {lon}")
                print(f"Altitude : {alt}")

                #
                # BUILD JSON PAYLOAD
                #

                payload = create_navsatfix_message(
                    lat,
                    lon,
                    alt
                )

                json_payload = json.dumps(payload)

                print("[JSON PAYLOAD]")
                print(json_payload)

                #
                # SEND TO WEBSOCKET
                #

                print("[INFO] Sending websocket message...")

                await ws.send(json_payload)

                print("[SUCCESS] Websocket message sent")

                #
                # OPTIONAL:
                # WAIT FOR SERVER RESPONSE
                #

                try:

                    response = await asyncio.wait_for(
                        ws.recv(),
                        timeout=0.1
                    )

                    print("[SERVER RESPONSE]")
                    print(response)

                except asyncio.TimeoutError:
                    print("[INFO] No websocket response from server")

                await asyncio.sleep(0.5)

    except Exception as e:

        print("\n[ERROR] Websocket connection failed!")
        print(str(e))

        print("\n[TRACEBACK]")
        traceback.print_exc()


if __name__ == "__main__":

    try:
        asyncio.run(telemetry_loop())

    except KeyboardInterrupt:
        print("\n[INFO] Shutting down cleanly")