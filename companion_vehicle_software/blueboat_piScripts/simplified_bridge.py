from pymavlink import mavutil
import asyncio
import websockets
import json
import time

master = mavutil.mavlink_connection(
    "udpin:0.0.0.0:14550"
)

master.wait_heartbeat()

async def bridge():

    async with websockets.connect(
        "ws://10.24.5.242:8000/ws/vehicle/blueboat-1"
    ) as ws:

        while True:

            msg = master.recv_match(
                type="GLOBAL_POSITION_INT",
                blocking=True
            )

            payload = {
                "vehicle_id": "blueboat-1",
                "vehicle_type": "usv",
                "topic": "/vehicles/blueboat-1/navsatfix",
                "type": "sensor_msgs/msg/NavSatFix",
                "stamp": time.time(),
                "msg": {
                    "latitude": msg.lat / 1e7,
                    "longitude": msg.lon / 1e7,
                    "altitude": msg.relative_alt / 1000.0
                }
            }

            await ws.send(json.dumps(payload))

asyncio.run(bridge())