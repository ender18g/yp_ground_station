#!/usr/bin/env python3
"""Local control agent that lets the web UI start/stop com_tcp_relay.py.

Browsers cannot launch host processes directly for security reasons, so this
small helper fills that gap: run it once on the host (Windows/Linux/macOS),
and the web UI can then start, stop, and list serial-to-TCP relays over a
local HTTP API instead of you typing `python services/com_tcp_relay.py ...`
in a terminal every time you want to switch a port.

Usage
-----
    pip install pyserial
    python services/relay_agent.py

Leave it running in the background (or add it to your OS startup items).
The web UI (served from a different origin) talks to it directly at
http://127.0.0.1:5757 -- it never goes through the Docker containers.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import serial.tools.list_ports
except ImportError:
    raise SystemExit("pyserial is required. Run: pip install pyserial")

AGENT_PORT = 5757
RELAY_SCRIPT = Path(__file__).resolve().parent / "com_tcp_relay.py"

_lock = threading.Lock()
_relays: dict[int, subprocess.Popen] = {}  # tcp_port -> relay subprocess


def _list_ports() -> list[dict[str, str]]:
    return [{"device": p.device, "description": p.description or ""} for p in serial.tools.list_ports.comports()]


def _start_relay(port: str, baud: int, tcp_port: int) -> int:
    with _lock:
        existing = _relays.get(tcp_port)
        if existing and existing.poll() is None:
            existing.terminate()
            existing.wait(timeout=5)
        proc = subprocess.Popen(
            [sys.executable, str(RELAY_SCRIPT), "--port", port, "--baud", str(baud), "--tcp-port", str(tcp_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _relays[tcp_port] = proc
        return proc.pid


def _stop_relay(tcp_port: int) -> bool:
    with _lock:
        proc = _relays.pop(tcp_port, None)
        if not proc:
            return False
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return True


def _list_relays() -> list[dict[str, Any]]:
    with _lock:
        return [{"tcp_port": tcp_port, "pid": proc.pid, "running": proc.poll() is None} for tcp_port, proc in _relays.items()]


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        # Loopback-only server; wildcard CORS is fine since nothing sensitive is exposed.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self._send_json(204, "")

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        elif self.path == "/ports":
            self._send_json(200, _list_ports())
        elif self.path == "/relays":
            self._send_json(200, _list_relays())
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/relays":
            self._send_json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            port = str(body["port"])
            baud = int(body.get("baud", 57600))
            tcp_port = int(body["tcp_port"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"invalid request: {exc}"})
            return
        try:
            pid = _start_relay(port, baud, tcp_port)
        except Exception as exc:
            self._send_json(500, {"error": str(exc)})
            return
        self._send_json(200, {"ok": True, "tcp_port": tcp_port, "pid": pid})

    def do_DELETE(self) -> None:
        if not self.path.startswith("/relays/"):
            self._send_json(404, {"error": "not found"})
            return
        try:
            tcp_port = int(self.path.rsplit("/", 1)[-1])
        except ValueError:
            self._send_json(400, {"error": "invalid tcp_port"})
            return
        self._send_json(200, {"ok": True, "stopped": _stop_relay(tcp_port)})

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[AGENT] {fmt % args}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", AGENT_PORT), Handler)
    print(f"[AGENT] Listening on http://127.0.0.1:{AGENT_PORT}")
    print("[AGENT] The web UI can now start/stop serial relays without a separate terminal.")
    print("[AGENT] Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[AGENT] Stopped.")
    finally:
        with _lock:
            for proc in _relays.values():
                if proc.poll() is None:
                    proc.terminate()


if __name__ == "__main__":
    main()
