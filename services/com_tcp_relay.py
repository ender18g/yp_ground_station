#!/usr/bin/env python3
"""Windows COM-port → TCP relay for RFD-900 / telemetry radios.

Runs on the Windows host (NOT inside Docker).  Opens a COM port and exposes
the raw MAVLink byte stream as a TCP server so the yp-server Docker container
can reach it at:

    tcp:host.docker.internal:<tcp-port>

Usage
-----
    pip install pyserial
    python services/com_tcp_relay.py --port COM12 --baud 57600 --tcp-port 5762

Then in the browser Connections panel → RFD-900 tab click
"Connect via Network tab" (or enter manually in the Network tab):

    tcp:host.docker.internal:5762

Only one Docker connection is served at a time; when it disconnects the relay
keeps the serial port open and waits for a new TCP connection automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    raise SystemExit(
        "pyserial is required. Run: pip install pyserial"
    )

DEFAULT_TCP_PORT = 5762


async def _pipe_tcp_to_serial(
    reader: asyncio.StreamReader,
    ser: serial.Serial,
) -> None:
    """Forward bytes from the TCP client (Docker) to the serial port."""
    loop = asyncio.get_running_loop()
    while True:
        data = await reader.read(4096)
        if not data:
            return  # client closed connection
        await loop.run_in_executor(None, ser.write, data)


async def _pipe_serial_to_tcp(
    writer: asyncio.StreamWriter,
    ser: serial.Serial,
) -> None:
    """Forward bytes from the serial port to the TCP client (Docker)."""
    loop = asyncio.get_running_loop()
    while True:
        data: bytes = await loop.run_in_executor(None, ser.read, 256)
        if data:
            writer.write(data)
            await writer.drain()
        else:
            await asyncio.sleep(0.005)


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ser: serial.Serial,
) -> None:
    addr = writer.get_extra_info("peername")
    print(f"[RELAY] Docker connected from {addr}")

    t_in = asyncio.create_task(_pipe_tcp_to_serial(reader, ser))
    t_out = asyncio.create_task(_pipe_serial_to_tcp(writer, ser))

    # Run until either direction fails/closes
    done, pending = await asyncio.wait(
        [t_in, t_out], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    print(f"[RELAY] Docker disconnected from {addr}")


async def _serve(port: str, baud: int, tcp_port: int) -> None:
    print(f"[RELAY] Opening {port} at {baud} baud …")
    try:
        ser = serial.Serial(port, baud, timeout=0)
    except serial.SerialException as exc:
        raise SystemExit(f"[ERROR] Cannot open {port}: {exc}") from exc

    print(f"[RELAY] Listening on 0.0.0.0:{tcp_port}")
    print()
    print(f"  In the browser Connections panel → Network tab enter:")
    print(f"    tcp:host.docker.internal:{tcp_port}")
    print()
    print("  Press Ctrl+C to stop.")

    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, ser),
        "0.0.0.0",
        tcp_port,
    )
    async with server:
        await server.serve_forever()


def _list_ports() -> None:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    print("Available serial ports:")
    for p in ports:
        print(f"  {p.device}  —  {p.description}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Windows COM-port → TCP relay for RFD-900 / telemetry radios. "
            "Run this on the Windows host; connect from Docker via "
            "tcp:host.docker.internal:<tcp-port>."
        ),
    )
    parser.add_argument(
        "--port",
        default="COM12",
        help="Windows COM port to open (default: COM12)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=57600,
        help="Serial baud rate (default: 57600)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        default=DEFAULT_TCP_PORT,
        help=f"Local TCP port to listen on (default: {DEFAULT_TCP_PORT})",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available COM ports and exit.",
    )
    args = parser.parse_args()

    if args.list_ports:
        _list_ports()
        return

    try:
        asyncio.run(_serve(args.port, args.baud, args.tcp_port))
    except KeyboardInterrupt:
        print("\n[RELAY] Stopped.")
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
