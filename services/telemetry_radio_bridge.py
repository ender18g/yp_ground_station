#!/usr/bin/env python3
"""Telemetry radio bridge for ArduPilot vehicle <-> web server.

This script scans for available serial ports, connects to a MAVLink stream on
an attached 900 MHz telemetry radio, forwards telemetry as vehicle messages to
YP server WebSocket /ws/vehicle/{vehicle_id}, and accepts commands from the
server to send back to the vehicle.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Optional

try:
    import serial.tools.list_ports
except ImportError as exc:
    raise SystemExit(
        "pyserial is required for serial port discovery. Install with `pip install pyserial`."
    ) from exc

try:
    from pymavlink import mavutil
except ImportError as exc:
    raise SystemExit(
        "pymavlink is required. Install with `pip install pymavlink`."
    ) from exc

try:
    import websockets
except ImportError as exc:
    raise SystemExit(
        "websockets is required. Install with `pip install websockets`."
    ) from exc

DEFAULT_BAUD = 57600
DEFAULT_SEND_HZ = 10.0
DEFAULT_WS_URL = "ws://localhost:8000/ws/vehicle"


def list_serial_ports() -> list[serial.tools.list_ports_common.ListPortInfo]:
    return list(serial.tools.list_ports.comports())


def print_port_list(ports: list[serial.tools.list_ports_common.ListPortInfo]) -> None:
    if not ports:
        print("No serial ports detected.")
        return

    print("Available serial ports:")
    for port in ports:
        print(
            f"  {port.device} - {port.description} - {port.hwid}"
        )


async def detect_mavlink_port(
    baud: int,
    timeout: float,
    verbose: bool = False,
) -> Optional[str]:
    ports = list_serial_ports()
    if verbose:
        print_port_list(ports)

    for port in ports:
        if verbose:
            print(f"Probing port {port.device} at {baud} baud...")

        try:
            master = await asyncio.to_thread(
                mavutil.mavlink_connection,
                f"serial:{port.device}:{baud}",
                source_system=255,
                autoreconnect=False,
            )
            await asyncio.to_thread(master.wait_heartbeat, timeout)
            master.close()
            print(f"Detected MAVLink heartbeat on {port.device}")
            return port.device
        except Exception:
            if verbose:
                print(f"No MAVLink heartbeat on {port.device}")

    return None


def create_navsatfix_message(
    vehicle_id: str,
    lat: float,
    lon: float,
    alt: float,
    heading: Optional[float] = None,
) -> dict[str, object]:
    now = time.time()
    sec = int(now)
    nanosec = int((now - sec) * 1e9)

    msg: dict[str, object] = {
        "header": {
            "stamp": {"sec": sec, "nanosec": nanosec},
            "frame_id": "map",
        },
        "status": {"status": 0, "service": 1},
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "position_covariance": [0.0] * 9,
        "position_covariance_type": 0,
    }
    if heading is not None:
        msg["heading"] = heading

    return {
        "vehicle_id": vehicle_id,
        "vehicle_type": "uav",
        "topic": f"/vehicles/{vehicle_id}/navsatfix",
        "type": "sensor_msgs/msg/NavSatFix",
        "stamp": now,
        "msg": msg,
    }


def build_mavlink_connection(port: str, baud: int) -> mavutil.mavlink_connection:
    return mavutil.mavlink_connection(f"serial:{port}:{baud}", source_system=255, autoreconnect=False)


def send_radio_command(master: mavutil.mavlink_connection, command: dict[str, object]) -> None:
    cmd_type = command.get("type")
    if cmd_type == "waypoint":
        target = command.get("target", {})
        lat = target.get("latitude")
        lon = target.get("longitude")
        alt = float(target.get("altitude") or 30.0)
        if lat is None or lon is None:
            print("[COMMAND] waypoint command missing latitude/lon")
            return

        mode_mapping = master.mode_mapping()
        if mode_mapping and "GUIDED" in mode_mapping:
            try:
                master.mav.set_mode_send(
                    master.target_system,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mode_mapping["GUIDED"],
                )
                time.sleep(0.1)
            except Exception as exc:
                print(f"[COMMAND] failed to request GUIDED mode: {exc}")

        try:
            master.mav.command_long_send(
                master.target_system,
                master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            time.sleep(0.1)
        except Exception as exc:
            print(f"[COMMAND] failed to arm vehicle: {exc}")

        try:
            master.mav.set_position_target_global_int_send(
                0,
                master.target_system,
                master.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                int(0b110111111000),
                int(float(lat) * 1e7),
                int(float(lon) * 1e7),
                float(alt),
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            print(f"[COMMAND] waypoint set: {lat}, {lon}, {alt}")
        except Exception as exc:
            print(f"[COMMAND] failed to send waypoint: {exc}")

    elif cmd_type == "rtb" or cmd_type == "return_to_launch":
        try:
            master.set_mode("RTL")
            print("[COMMAND] requested RTL mode")
        except Exception:
            try:
                master.mav.command_long_send(
                    master.target_system,
                    master.target_component,
                    mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                )
                print("[COMMAND] sent RTL command_long")
            except Exception as exc:
                print(f"[COMMAND] failed to send RTL: {exc}")

    elif cmd_type == "arm":
        try:
            master.mav.command_long_send(
                master.target_system,
                master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                1,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            print("[COMMAND] arm command sent")
        except Exception as exc:
            print(f"[COMMAND] failed to arm: {exc}")

    elif cmd_type == "disarm":
        try:
            master.mav.command_long_send(
                master.target_system,
                master.target_component,
                mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            )
            print("[COMMAND] disarm command sent")
        except Exception as exc:
            print(f"[COMMAND] failed to disarm: {exc}")

    elif cmd_type == "mode":
        mode_name = str(command.get("mode", "")).upper()
        if not mode_name:
            print("[COMMAND] mode command missing mode name")
            return
        mapping = master.mode_mapping()
        if mapping and mode_name in mapping:
            try:
                master.mav.set_mode_send(
                    master.target_system,
                    mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
                    mapping[mode_name],
                )
                print(f"[COMMAND] requested mode {mode_name}")
            except Exception as exc:
                print(f"[COMMAND] failed to set mode {mode_name}: {exc}")
        else:
            print(f"[COMMAND] mode {mode_name} not supported by vehicle")

    else:
        print(f"[COMMAND] unsupported command type: {cmd_type}")


async def handle_server_messages(
    websocket: websockets.WebSocketClientProtocol,
    master: mavutil.mavlink_connection,
    vehicle_id: str,
) -> None:
    while True:
        raw = await websocket.recv()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            print("[WEBSOCKET] received invalid JSON from server")
            continue

        op = payload.get("op")
        if op != "command":
            continue

        if payload.get("vehicle_id") not in (None, vehicle_id):
            continue

        command = payload.get("command") or payload
        if not isinstance(command, dict):
            print("[WEBSOCKET] command payload missing or invalid")
            continue

        print(f"[WEBSOCKET] received command: {command}")
        await asyncio.to_thread(send_radio_command, master, command)


async def read_mavlink_telemetry(
    websocket: websockets.WebSocketClientProtocol,
    master: mavutil.mavlink_connection,
    vehicle_id: str,
) -> None:
    print("[INFO] requesting telemetry stream from vehicle")
    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_POSITION,
        int(DEFAULT_SEND_HZ),
        1,
    )

    while True:
        msg = await asyncio.to_thread(
            master.recv_match,
            type="GLOBAL_POSITION_INT",
            blocking=True,
            timeout=5,
        )
        if msg is None:
            print("[WARN] no GLOBAL_POSITION_INT message received")
            continue

        lat = msg.lat / 1e7
        lon = msg.lon / 1e7
        alt = msg.relative_alt / 1000.0
        heading = getattr(msg, "hdg", None)
        if heading is not None:
            heading = heading / 100.0

        payload = create_navsatfix_message(vehicle_id, lat, lon, alt, heading)
        try:
            await websocket.send(json.dumps(payload))
            print("[INFO] sent navsatfix telemetry")
        except Exception as exc:
            print(f"[ERROR] failed to send telemetry: {exc}")
            raise


async def run_bridge(args: argparse.Namespace) -> None:
    if args.list_ports:
        print_port_list(list_serial_ports())
        return

    serial_port = args.serial_port
    if not serial_port:
        serial_port = await detect_mavlink_port(args.baud, args.detect_timeout, verbose=args.verbose)
        if serial_port is None:
            print("Unable to detect a MAVLink port automatically.")
            print_port_list(list_serial_ports())
            raise SystemExit("Specify a serial port using --serial-port")

    print(f"[INFO] using serial port {serial_port} at {args.baud} baud")
    master = await asyncio.to_thread(build_mavlink_connection, serial_port, args.baud)

    print("[INFO] waiting for MAVLink heartbeat...")
    await asyncio.to_thread(master.wait_heartbeat, args.heartbeat_timeout)
    print(f"[INFO] heartbeat received from system {master.target_system}.{master.target_component}")

    websocket_url = args.server_ws_url.rstrip("/") + "/" + args.vehicle_id
    print(f"[INFO] connecting to server websocket: {websocket_url}")

    async with websockets.connect(websocket_url, ping_interval=10, ping_timeout=10) as ws:
        print("[INFO] websocket connected")
        await asyncio.gather(
            read_mavlink_telemetry(ws, master, args.vehicle_id),
            handle_server_messages(ws, master, args.vehicle_id),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Telemetry radio bridge for YP ground station and ArduPilot.",
    )
    parser.add_argument(
        "--serial-port",
        help="Serial port for the 900 MHz telemetry radio (e.g. /dev/ttyUSB0).",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUD,
        help=f"Serial baud rate (default: {DEFAULT_BAUD}).",
    )
    parser.add_argument(
        "--vehicle-id",
        default="telemetry-radio-001",
        help="Vehicle ID exposed to the web server.",
    )
    parser.add_argument(
        "--server-ws-url",
        default=DEFAULT_WS_URL,
        help="Base WebSocket URL for the web server vehicle endpoint.",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit.",
    )
    parser.add_argument(
        "--detect-timeout",
        type=float,
        default=3.0,
        help="Seconds to wait for a heartbeat while probing serial ports.",
    )
    parser.add_argument(
        "--heartbeat-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the flight controller heartbeat on the selected port.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra serial port discovery output.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_bridge(args))
    except KeyboardInterrupt:
        print("\n[INFO] shutdown requested")
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
