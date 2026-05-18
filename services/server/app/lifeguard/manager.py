"""Lifeguard mission manager: connects to drones, executes SAR missions, and streams
status events back to the async FastAPI server via an asyncio.Queue."""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from typing import Any, Optional

from .mavlink_controller import MavlinkController

log = logging.getLogger(__name__)

_SURFACE_FRAMES = {"USV", "UGV", "Ship"}
_SHIP_TRACK_INTERVAL_S = 10.0


class LifeguardManager:
    """Manages MAVLink agent connections and executes SAR missions.

    All MAVLink I/O is synchronous and runs in background daemon threads.
    Status events are pushed to *event_queue* (an asyncio.Queue) using
    loop.call_soon_threadsafe so the FastAPI server can broadcast them to
    connected browser clients without blocking the event loop.
    """

    def __init__(
        self,
        settings: dict[str, Any],
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._settings = settings
        self._event_queue = event_queue
        self._loop = loop

        self._controllers: dict[str, MavlinkController] = {}
        self._frame_types: dict[str, str] = {}      # agent_id → frame type string
        self._active_missions: dict[str, Optional[str]] = {}  # agent_id → mission type or None

        track_min = int(settings.get("ship", {}).get("track_history_minutes", 30))
        track_maxlen = max(10, int(track_min * 60 / _SHIP_TRACK_INTERVAL_S))
        self._ship_track: deque[tuple[float, float]] = deque(maxlen=track_maxlen)
        self._ship_track_lock = threading.Lock()
        self._last_ship_track_update: float = 0.0

        self._ship_return_stops: dict[str, threading.Event] = {}

        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Connect all agents from config and start the MAVLink poll loop."""
        for agent_cfg in self._settings.get("agents", []):
            name = agent_cfg.get("name", "")
            conn_str = agent_cfg.get("connection_string", "").strip()
            frame_type = agent_cfg.get("frame_type", "UAV")
            if name and conn_str:
                self._connect_agent(name, conn_str, frame_type)

        self._poll_thread = threading.Thread(
            target=self._run_poll_loop, name="lifeguard-poll", daemon=True
        )
        self._poll_thread.start()

    def stop(self) -> None:
        """Signal the poll loop to stop and close all MAVLink connections."""
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=3)
        for ctrl in self._controllers.values():
            try:
                ctrl.close_connection()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Ship track (fed from yp_gps service via the FastAPI server)
    # ------------------------------------------------------------------

    def update_ship_position(self, lat: float, lon: float) -> None:
        """Record the ship's position. Called from the async event loop; throttled
        to one sample per *_SHIP_TRACK_INTERVAL_S* seconds."""
        now = time.time()
        if now - self._last_ship_track_update < _SHIP_TRACK_INTERVAL_S:
            return
        with self._ship_track_lock:
            self._ship_track.append((lat, lon))
        self._last_ship_track_update = now

    def _get_ship_position(self) -> Optional[dict[str, float]]:
        with self._ship_track_lock:
            if self._ship_track:
                lat, lon = self._ship_track[-1]
                return {"lat": lat, "lon": lon, "alt": 0.0}
        return None

    def _get_ship_track_copy(self) -> list[tuple[float, float]]:
        with self._ship_track_lock:
            return list(self._ship_track)

    # ------------------------------------------------------------------
    # Public command interface (called from async handlers via to_thread)
    # ------------------------------------------------------------------

    def connect_agent(
        self, name: str, connection_string: str, frame_type: str = "UAV"
    ) -> None:
        """Connect a new agent and broadcast updated agent list."""
        self._connect_agent(name, connection_string, frame_type)

    def disconnect_agent(self, name: str) -> None:
        ctrl = self._controllers.pop(name, None)
        self._frame_types.pop(name, None)
        self._active_missions.pop(name, None)
        if ctrl:
            try:
                ctrl.close_connection()
            except Exception:
                pass
        self._post_agents()

    def execute_grid_search(
        self,
        agent_id: str,
        lat: float,
        lon: float,
        grid_size_m: float,
        swath_m: float,
        altitude_m: float,
    ) -> None:
        ctrl = self._get_controller(agent_id)
        if not ctrl:
            self._post_status(agent_id, f"Agent '{agent_id}' is not connected.", "error")
            return
        if self._active_missions.get(agent_id):
            self._post_status(agent_id, f"Agent '{agent_id}' already has an active mission.", "warn")
            return
        self._active_missions[agent_id] = "grid_search"
        self._post_agents()
        threading.Thread(
            target=self._run_grid_search,
            args=(agent_id, ctrl, lat, lon, grid_size_m, swath_m, altitude_m),
            daemon=True,
            name=f"grid-{agent_id}",
        ).start()

    def execute_mob(self) -> None:
        """Dispatch the first available aerial/surface agent on a MOB search."""
        track = self._get_ship_track_copy()
        if not track:
            self._post_status(None, "No ship track available — cannot execute MOB search.", "error")
            return

        ship_cfg = self._settings.get("ship", {})
        mission_cfg = self._settings.get("mission", {})

        agent_id = self._pick_idle_agent()
        if not agent_id:
            self._post_status(None, "No idle agents available for MOB search.", "error")
            return

        ctrl = self._controllers[agent_id]
        self._active_missions[agent_id] = "mob"
        self._post_agents()
        self._post_status(agent_id, f"MOB: dispatching {agent_id} on parallel-track search.", "info")

        threading.Thread(
            target=self._run_mob_search,
            args=(
                agent_id, ctrl, track,
                float(ship_cfg.get("mob_corridor_half_width_m", 50.0)),
                float(mission_cfg.get("default_swath_width", 20.0)),
                float(mission_cfg.get("default_waypoint_altitude", 30.0)),
                float(ship_cfg.get("mob_takeoff_altitude_m", 100.0)),
                float(ship_cfg.get("mob_climb_speed_ms", 8.0)),
            ),
            daemon=True,
            name=f"mob-{agent_id}",
        ).start()

    def execute_fly_to(
        self, agent_id: str, lat: float, lon: float, altitude_m: float
    ) -> None:
        ctrl = self._get_controller(agent_id)
        if not ctrl:
            self._post_status(agent_id, f"Agent '{agent_id}' is not connected.", "error")
            return
        threading.Thread(
            target=self._run_fly_to,
            args=(agent_id, ctrl, lat, lon, altitude_m),
            daemon=True,
            name=f"flyto-{agent_id}",
        ).start()

    def execute_rtb(self, agent_id: str) -> None:
        """Return agent to ship position via continuous tracking."""
        ctrl = self._get_controller(agent_id)
        if not ctrl:
            self._post_status(agent_id, f"Agent '{agent_id}' is not connected.", "error")
            return
        pos = ctrl.get_current_position()
        alt = (
            pos["alt"]
            if pos
            else float(self._settings.get("mission", {}).get("default_waypoint_altitude", 30.0))
        )
        self._start_ship_return_tracking(agent_id, ctrl, alt)
        self._post_status(agent_id, f"{agent_id} returning to ship.", "info")

    def get_agent_states(self) -> list[dict[str, Any]]:
        result = []
        for aid, ctrl in self._controllers.items():
            result.append({
                "id": aid,
                "frame_type": self._frame_types.get(aid, "UAV"),
                "connected": ctrl.is_connected(),
                "active_mission": self._active_missions.get(aid),
            })
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect_agent(
        self, name: str, connection_string: str, frame_type: str
    ) -> None:
        baud = self._settings.get("mavlink", {}).get("baudrate")
        src_id = self._settings.get("mavlink", {}).get("source_system_id", 255)
        ctrl = MavlinkController(connection_string, baud, src_id)
        try:
            ctrl.connect()
            self._controllers[name] = ctrl
            self._frame_types[name] = frame_type
            self._active_missions[name] = None
            self._post_status(name, f"[{name}] Connected.", "info")
        except Exception as exc:
            log.error(f"Lifeguard: could not connect to '{name}': {exc}")
            self._post_status(name, f"[{name}] Connection failed: {exc}", "error")
        self._post_agents()

    def _get_controller(self, agent_id: str) -> Optional[MavlinkController]:
        ctrl = self._controllers.get(agent_id)
        return ctrl if ctrl and ctrl.is_connected() else None

    def _pick_idle_agent(self) -> Optional[str]:
        for aid, ctrl in self._controllers.items():
            if (
                self._frame_types.get(aid, "UAV") not in ("Ship",)
                and ctrl.is_connected()
                and not self._active_missions.get(aid)
            ):
                return aid
        return None

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6_378_137.0
        la1, la2 = math.radians(lat1), math.radians(lat2)
        dlo = math.radians(lon2 - lon1)
        dlat = la2 - la1
        a = math.sin(dlat / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
        return R * 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))

    def _post(self, event: dict[str, Any]) -> None:
        """Thread-safe: post an event onto the asyncio event queue."""
        try:
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        except Exception:
            pass

    def _post_status(
        self,
        agent_id: Optional[str],
        message: str,
        level: str = "info",
    ) -> None:
        log.info(f"Lifeguard [{agent_id or 'system'}]: {message}")
        self._post({
            "op": "lifeguard_status",
            "agent_id": agent_id,
            "message": message,
            "level": level,
            "stamp": time.time(),
        })

    def _post_agents(self) -> None:
        self._post({"op": "lifeguard_agents", "agents": self.get_agent_states()})

    def _post_vehicle_position(
        self,
        agent_id: str,
        lat: float,
        lon: float,
        alt: float,
        heading_deg: Optional[float],
        frame_type: str,
    ) -> None:
        """Post a vehicle position so the main server can update the shared vehicles dict."""
        vehicle_type = "usv" if frame_type in _SURFACE_FRAMES else "uav"
        now = time.time()
        sec = int(now)
        nanosec = int((now - sec) * 1e9)
        self._post({
            "op": "lifeguard_vehicle_update",
            "payload": {
                "vehicle_id": agent_id,
                "vehicle_type": vehicle_type,
                "topic": f"/vehicles/{agent_id}/navsatfix",
                "type": "sensor_msgs/msg/NavSatFix",
                "stamp": now,
                "msg": {
                    "header": {
                        "stamp": {"sec": sec, "nanosec": nanosec},
                        "frame_id": "lifeguard",
                    },
                    "status": {"status": 0, "service": 1},
                    "latitude": lat,
                    "longitude": lon,
                    "altitude": alt,
                    "heading": heading_deg if heading_deg is not None else 0.0,
                },
            },
        })
        if heading_deg is not None:
            self._post({
                "op": "lifeguard_vehicle_update",
                "payload": {
                    "vehicle_id": agent_id,
                    "vehicle_type": vehicle_type,
                    "topic": f"/vehicles/{agent_id}/pose",
                    "type": "geometry_msgs/msg/Pose",
                    "stamp": now,
                    "msg": {
                        "position": {"x": 0.0, "y": 0.0, "z": alt},
                        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
                        "heading": heading_deg,
                    },
                },
            })

    # ------------------------------------------------------------------
    # Mission execution threads
    # ------------------------------------------------------------------

    def _run_grid_search(
        self,
        agent_id: str,
        ctrl: MavlinkController,
        lat: float,
        lon: float,
        grid_size_m: float,
        swath_m: float,
        altitude_m: float,
    ) -> None:
        ctrl._mavlink_busy.set()
        try:
            frame = self._frame_types.get(agent_id, "UAV")
            is_surface = frame in _SURFACE_FRAMES
            ship_cfg = self._settings.get("ship", {})
            takeoff_alt = float(ship_cfg.get("mob_takeoff_altitude_m", 100.0))
            climb_spd = float(ship_cfg.get("mob_climb_speed_ms", 8.0))

            self._post_status(agent_id, f"[{agent_id}] Generating {grid_size_m:.0f} m grid mission…")
            path = ctrl.generate_and_upload_search_grid_mission(
                lat, lon, grid_size_m, swath_m, altitude_m,
                include_takeoff=not is_surface,
                takeoff_altitude_m=takeoff_alt,
                climb_speed_ms=climb_spd,
            )
            if path is None:
                raise RuntimeError("Mission upload failed.")

            self._post_status(agent_id, f"[{agent_id}] Mission uploaded ({len(path)} waypoints).")
            if path:
                self._post({"op": "lifeguard_path", "agent_id": agent_id, "path": path})

            self._arm_and_start(agent_id, ctrl, force_arm=not is_surface)
        except Exception as exc:
            self._post_status(agent_id, f"[{agent_id}] Grid search failed: {exc}", "error")
        finally:
            self._active_missions[agent_id] = None
            ctrl._mavlink_busy.clear()
            self._post_agents()

    def _run_mob_search(
        self,
        agent_id: str,
        ctrl: MavlinkController,
        track: list[tuple[float, float]],
        corridor_half_width_m: float,
        swath_m: float,
        altitude_m: float,
        takeoff_altitude_m: float,
        climb_speed_ms: float,
    ) -> None:
        ctrl._mavlink_busy.set()
        try:
            frame = self._frame_types.get(agent_id, "UAV")
            is_surface = frame in _SURFACE_FRAMES
            mob_alt = 0.0 if is_surface else altitude_m

            self._post_status(agent_id, f"[{agent_id}] Generating MOB curved-track mission…")
            path = ctrl.generate_and_upload_mob_search_mission(
                track, corridor_half_width_m, swath_m, mob_alt,
                takeoff_altitude_m, climb_speed_ms,
                include_takeoff=not is_surface,
            )
            if path is None:
                raise RuntimeError("MOB mission upload failed.")

            self._post_status(agent_id, f"[{agent_id}] MOB mission uploaded ({len(path)} waypoints).")
            if path:
                self._post({"op": "lifeguard_path", "agent_id": agent_id, "path": path})

            self._arm_and_start(agent_id, ctrl, force_arm=True)
        except Exception as exc:
            self._post_status(agent_id, f"[{agent_id}] MOB search failed: {exc}", "error")
        finally:
            self._active_missions[agent_id] = None
            ctrl._mavlink_busy.clear()
            self._post_agents()

    def _run_fly_to(
        self,
        agent_id: str,
        ctrl: MavlinkController,
        lat: float,
        lon: float,
        altitude_m: float,
    ) -> None:
        ok = ctrl.fly_to(lat, lon, altitude_m)
        level = "info" if ok else "error"
        msg = (
            f"[{agent_id}] Flying to ({lat:.6f}, {lon:.6f}) at {altitude_m:.1f} m."
            if ok
            else f"[{agent_id}] Fly-to command failed."
        )
        self._post_status(agent_id, msg, level)

    def _arm_and_start(
        self, agent_id: str, ctrl: MavlinkController, force_arm: bool
    ) -> None:
        if ctrl.set_mode("GUIDED"):
            self._post_status(agent_id, f"[{agent_id}] GUIDED mode set.")
        time.sleep(1.5)

        if not ctrl.arm_vehicle(force=force_arm):
            raise RuntimeError("Arming failed.")
        self._post_status(agent_id, f"[{agent_id}] Armed.")
        time.sleep(0.5)

        if ctrl.set_mode("AUTO"):
            self._post_status(agent_id, f"[{agent_id}] AUTO mode set.")
        time.sleep(1.0)

        if not ctrl.start_mission():
            raise RuntimeError("Mission start failed.")
        self._post_status(agent_id, f"[{agent_id}] Mission started.")
        try:
            ctrl.start_position_stream(rate_hz=5)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Ship-return tracking (post-target-found)
    # ------------------------------------------------------------------

    def _start_ship_return_tracking(
        self, agent_id: str, ctrl: MavlinkController, alt: float
    ) -> None:
        old = self._ship_return_stops.pop(agent_id, None)
        if old:
            old.set()

        stop = threading.Event()
        self._ship_return_stops[agent_id] = stop

        UPDATE_S = 5.0
        ARRIVAL_M = 25.0
        MOVE_M = 10.0

        def _track() -> None:
            last_lat: Optional[float] = None
            last_lon: Optional[float] = None
            while not stop.is_set():
                try:
                    ship = self._get_ship_position()
                    if not ship:
                        stop.wait(UPDATE_S)
                        continue
                    s_lat, s_lon = ship["lat"], ship["lon"]
                    if ctrl.is_connected():
                        pos = ctrl.get_current_position()
                        if pos:
                            if self._haversine_m(pos["lat"], pos["lon"], s_lat, s_lon) < ARRIVAL_M:
                                self._post_status(agent_id, f"[{agent_id}] Arrived at ship.")
                                break
                    should_send = last_lat is None or self._haversine_m(last_lat, last_lon, s_lat, s_lon) >= MOVE_M
                    if should_send and ctrl.is_connected():
                        ctrl.fly_to(s_lat, s_lon, alt)
                        last_lat, last_lon = s_lat, s_lon
                except Exception:
                    log.exception(f"Ship-return tracking error for {agent_id}")
                stop.wait(UPDATE_S)
            self._ship_return_stops.pop(agent_id, None)

        threading.Thread(target=_track, daemon=True, name=f"ship-return-{agent_id}").start()

    # ------------------------------------------------------------------
    # MAVLink poll loop
    # ------------------------------------------------------------------

    def _run_poll_loop(self) -> None:
        """Background thread: drain MAVLink messages for all connected agents,
        publish position updates, and handle FOUND/STATUSTEXT events."""
        last_poll = 0.0
        POLL_INTERVAL = 0.2

        while not self._stop_event.is_set():
            now = time.time()
            if now - last_poll >= POLL_INTERVAL:
                for aid, ctrl in list(self._controllers.items()):
                    if not ctrl.is_connected() or ctrl._mavlink_busy.is_set():
                        continue
                    try:
                        latest_lat = latest_lon = latest_alt = None
                        latest_hdg = 65535
                        drained = 0
                        while drained < 30:
                            msg = ctrl.master.recv_match(blocking=False)
                            if not msg:
                                break
                            drained += 1
                            mtype = msg.get_type()

                            if mtype == "GLOBAL_POSITION_INT":
                                latest_lat = msg.lat / 1e7
                                latest_lon = msg.lon / 1e7
                                latest_alt = msg.relative_alt / 1000.0
                                latest_hdg = msg.hdg

                            elif mtype == "STATUSTEXT" and hasattr(msg, "text"):
                                raw = msg.text
                                if isinstance(raw, (bytes, bytearray)):
                                    raw = raw.decode("utf-8", errors="ignore").rstrip("\x00")
                                if isinstance(raw, str) and raw:
                                    if raw.startswith("HANDSHAKE_REQ:"):
                                        seq = raw.split(":", 2)[1] if ":" in raw else "0"
                                        try:
                                            ctrl.send_status_text(f"HANDSHAKE_ACK:{seq}")
                                        except Exception:
                                            pass
                                    elif raw.upper().startswith("FOUND:"):
                                        try:
                                            coords = raw.split(":", 1)[1]
                                            la_s, lo_s = coords.split(",", 1)
                                            self._handle_found_target(aid, float(la_s), float(lo_s))
                                        except (ValueError, IndexError):
                                            log.warning(f"Unparseable FOUND text: {raw!r}")
                                    else:
                                        self._post_status(aid, f"[{aid}] {raw}")

                        if latest_lat is not None:
                            hdg = (latest_hdg / 100.0) if latest_hdg != 65535 else None
                            self._post_vehicle_position(
                                aid, latest_lat, latest_lon, latest_alt or 0.0,
                                hdg, self._frame_types.get(aid, "UAV"),
                            )
                    except Exception as exc:
                        log.warning(f"Lifeguard poll error for {aid}: {exc}")
                last_poll = now

            time.sleep(0.05)

    # ------------------------------------------------------------------
    # FOUND target handling
    # ------------------------------------------------------------------

    def _handle_found_target(
        self, source_agent_id: str, lat: float, lon: float
    ) -> None:
        """Called when an agent's STATUSTEXT begins with 'FOUND:'.

        The source agent returns to ship while a second idle agent is dispatched
        to verify the reported location with a small grid search.
        """
        self._post_status(
            source_agent_id,
            f"[{source_agent_id}] Target reported at ({lat:.6f}, {lon:.6f}).",
            "info",
        )
        mission_cfg = self._settings.get("mission", {})
        default_alt = float(mission_cfg.get("default_waypoint_altitude", 30.0))
        swath = float(mission_cfg.get("default_swath_width", 20.0))

        source_ctrl = self._controllers.get(source_agent_id)
        if source_ctrl and source_ctrl.is_connected():
            source_ctrl.set_mode("GUIDED")
            pos = source_ctrl.get_current_position()
            alt = pos["alt"] if pos else default_alt
            self._start_ship_return_tracking(source_agent_id, source_ctrl, alt)
            self._post_status(source_agent_id, f"[{source_agent_id}] Returning to ship.")
        self._active_missions[source_agent_id] = None
        self._post_agents()

        # Dispatch a second agent to perform a tight verification grid.
        verifier_id = None
        for aid, ctrl in self._controllers.items():
            if (
                aid != source_agent_id
                and self._frame_types.get(aid, "UAV") != "Ship"
                and ctrl.is_connected()
                and not self._active_missions.get(aid)
            ):
                verifier_id = aid
                break

        if not verifier_id:
            self._post_status(None, "No second agent available to verify target.", "warn")
            return

        verify_alt = max(15.0, default_alt - 5.0)
        self._post_status(verifier_id, f"Dispatching {verifier_id} to verify target.", "info")
        self.execute_grid_search(verifier_id, lat, lon, 50, swath, verify_alt)
