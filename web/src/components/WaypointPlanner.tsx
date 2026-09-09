import {
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { Trash2 } from "lucide-react";
import { WaypointScene } from "./3d/WaypointScene";
import type { Command, RelativeWaypoint, Vehicle } from "../types";

type LocalWaypoint = { id: string; x: number; y: number; z: number };

export function WaypointPlanner({
  yp,
  vehicles,
  onCommand,
}: {
  yp?: Vehicle;
  vehicles: Vehicle[];
  onCommand: (vehicleId: string, cmd: Command) => void;
}) {
  const [waypoints, setWaypoints] = useState<LocalWaypoint[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedVehicleId, setSelectedVehicleId] = useState("");
  const updateWaypoint = (id: string, updates: Partial<LocalWaypoint>) =>
    setWaypoints((items) =>
      items.map((item) => (item.id === id ? { ...item, ...updates } : item)),
    );
  const deleteWaypoint = (id: string) => {
    setWaypoints((items) => items.filter((item) => item.id !== id));
    if (selectedId === id) setSelectedId(null);
  };
  const dispatch = () => {
    if (!yp?.position || yp.heading == null) {
      alert("Cannot dispatch: YP GPS or heading is unavailable.");
      return;
    }
    if (!waypoints.length) {
      alert("Add at least one waypoint before dispatching.");
      return;
    }
    if (!selectedVehicleId) {
      alert("Please select a vehicle to dispatch.");
      return;
    }
    const localWaypoints: RelativeWaypoint[] = waypoints.map(({ x, y, z }) => ({
      x,
      y,
      z,
    }));
    onCommand(selectedVehicleId, {
      type: "ship_relative_trajectory",
      ship_vehicle_id: yp.vehicle_id,
      local_waypoints: localWaypoints,
      arrival_radius_m: 6,
      update_hz: 10,
    });
    alert(
      `Dispatched ${localWaypoints.length} ship-relative waypoints to ${selectedVehicleId}`,
    );
    setWaypoints([]);
    setSelectedId(null);
  };
  return (
    <div
      className="planner-container"
      style={{
        display: "flex",
        flexDirection: "column",
        width: "100%",
        height: "100%",
        overflow: "hidden",
        backgroundColor: "#0f172a",
        color: "white",
        paddingTop: 60,
      }}
    >
      <div
        style={{
          flex: "2 1 0",
          minHeight: 0,
          position: "relative",
          borderBottom: "2px solid #334155",
          overflow: "hidden",
        }}
      >
        <WaypointScene waypoints={waypoints} selectedId={selectedId} />
      </div>
      <div
        style={{
          flex: "3 1 0",
          minHeight: 0,
          display: "flex",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            flex: "1 1 0",
            minWidth: 0,
            padding: 20,
            borderRight: "2px solid #334155",
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              marginBottom: 10,
            }}
          >
            <h2 style={{ fontSize: "1.2rem", fontWeight: "bold" }}>
              Lateral Planner (Top-Down)
            </h2>
            <button
              onClick={() => selectedId && deleteWaypoint(selectedId)}
              disabled={!selectedId}
              style={{
                padding: "4px 8px",
                background: "#ef4444",
                color: "white",
                border: "none",
                borderRadius: 4,
                opacity: selectedId ? 1 : 0,
                pointerEvents: selectedId ? "auto" : "none",
              }}
            >
              <Trash2 size={14} /> Delete Selected
            </button>
          </div>
          <div
            style={{
              flex: 1,
              minHeight: 0,
              display: "flex",
              justifyContent: "center",
              alignItems: "center",
              overflow: "hidden",
            }}
          >
            <InteractiveWaypoint2D
              waypoints={waypoints}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onAdd={(x, y) => {
                const id = Date.now().toString();
                setWaypoints((items) => [...items, { id, x, y, z: 15 }]);
                setSelectedId(id);
              }}
              onUpdate={updateWaypoint}
            />
          </div>
        </div>
        <div
          style={{
            flex: "1 1 0",
            minWidth: 0,
            padding: 20,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
          }}
        >
          <h2
            style={{ fontSize: "1.2rem", fontWeight: "bold", marginBottom: 10 }}
          >
            Altitude Profile
          </h2>
          <div
            style={{
              flex: 1,
              minHeight: 0,
              backgroundColor: "#1e293b",
              borderRadius: 8,
              border: "1px solid #475569",
              position: "relative",
              marginBottom: 15,
              padding: "10px 0",
              overflow: "hidden",
            }}
          >
            <AltitudeProfile
              waypoints={waypoints}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onUpdateAltitude={(id, z) => updateWaypoint(id, { z })}
            />
          </div>
          <div style={{ display: "flex", gap: 10 }}>
            <select
              value={selectedVehicleId}
              onChange={(event) => setSelectedVehicleId(event.target.value)}
              style={{
                flex: 1,
                background: "#1e293b",
                color: "white",
                border: "1px solid #475569",
                padding: 10,
                borderRadius: 4,
              }}
            >
              <option value="">-- Select Vehicle --</option>
              {vehicles.map((vehicle) => (
                <option key={vehicle.vehicle_id} value={vehicle.vehicle_id}>
                  {vehicle.vehicle_id} ({vehicle.vehicle_type})
                </option>
              ))}
            </select>
            <button
              onClick={() => {
                setWaypoints([]);
                setSelectedId(null);
              }}
              style={{
                padding: 10,
                background: "#475569",
                color: "white",
                border: "none",
                borderRadius: 4,
              }}
            >
              Clear All
            </button>
            <button
              onClick={dispatch}
              style={{
                padding: 10,
                background: "#2563eb",
                color: "white",
                border: "none",
                borderRadius: 4,
                fontWeight: "bold",
              }}
            >
              Dispatch
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function InteractiveWaypoint2D({
  waypoints,
  selectedId,
  onSelect,
  onAdd,
  onUpdate,
}: {
  waypoints: LocalWaypoint[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAdd: (x: number, y: number) => void;
  onUpdate: (id: string, updates: Partial<LocalWaypoint>) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const width = 150;
  const height = 150;
  const add = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.target !== ref.current || !ref.current) return;
    const rect = ref.current.getBoundingClientRect();
    onAdd(
      ((event.clientX - rect.left) / rect.width - 0.5) * width,
      -((event.clientY - rect.top) / rect.height - 0.5) * height,
    );
  };
  const drag = (id: string, event: ReactPointerEvent<HTMLDivElement>) => {
    event.stopPropagation();
    onSelect(id);
    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = (item: PointerEvent) => {
      if (!ref.current) return;
      const rect = ref.current.getBoundingClientRect();
      const x = Math.max(0, Math.min(item.clientX - rect.left, rect.width));
      const y = Math.max(0, Math.min(item.clientY - rect.top, rect.height));
      onUpdate(id, {
        x: (x / rect.width - 0.5) * width,
        y: -(y / rect.height - 0.5) * height,
      });
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };
  return (
    <div
      ref={ref}
      onPointerDown={add}
      style={{
        width: "100%",
        maxWidth: 400,
        aspectRatio: "1/1",
        backgroundColor: "#0f172a",
        backgroundImage:
          "linear-gradient(#334155 1px, transparent 1px), linear-gradient(90deg, #334155 1px, transparent 1px)",
        backgroundSize: "20px 20px",
        position: "relative",
        cursor: "crosshair",
        border: "2px solid #475569",
        borderRadius: 4,
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: "50%",
          left: "50%",
          width: `${(8 / width) * 100}%`,
          height: `${(33 / height) * 100}%`,
          transform: "translate(-50%, -50%)",
          backgroundImage: `url('${import.meta.env.BASE_URL}logos/YP.png')`,
          backgroundSize: "contain",
          backgroundPosition: "center",
          backgroundRepeat: "no-repeat",
          pointerEvents: "none",
          opacity: 0.8,
        }}
      />
      {waypoints.map((waypoint, index) => (
        <div
          key={waypoint.id}
          onPointerDown={(event) => drag(waypoint.id, event)}
          style={{
            position: "absolute",
            left: `${(waypoint.x / width + 0.5) * 100}%`,
            top: `${(-waypoint.y / height + 0.5) * 100}%`,
            width: 18,
            height: 18,
            backgroundColor: waypoint.id === selectedId ? "#38bdf8" : "#ef4444",
            border:
              waypoint.id === selectedId
                ? "2px solid white"
                : "1px solid #7f1d1d",
            borderRadius: "50%",
            transform: "translate(-50%, -50%)",
            cursor: "grab",
            zIndex: waypoint.id === selectedId ? 10 : 1,
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            fontSize: 10,
            color: "white",
            fontWeight: "bold",
            userSelect: "none",
          }}
        >
          {index + 1}
        </div>
      ))}
    </div>
  );
}

function AltitudeProfile({
  waypoints,
  selectedId,
  onSelect,
  onUpdateAltitude,
}: {
  waypoints: LocalWaypoint[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onUpdateAltitude: (id: string, altitude: number) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const max = 50;
  const drag = (id: string, event: ReactPointerEvent<HTMLDivElement>) => {
    event.stopPropagation();
    onSelect(id);
    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = (item: PointerEvent) => {
      if (!ref.current) return;
      const rect = ref.current.getBoundingClientRect();
      onUpdateAltitude(
        id,
        Math.max(
          0,
          (1 -
            Math.max(0, Math.min(item.clientY - rect.top, rect.height)) /
              rect.height) *
            max,
        ),
      );
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  };
  if (!waypoints.length)
    return (
      <div
        style={{
          padding: 20,
          color: "#64748b",
          textAlign: "center",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        Click the 2D map to add waypoints.
      </div>
    );
  const points = waypoints.map((waypoint, index) => ({
    ...waypoint,
    x: waypoints.length === 1 ? 50 : (index / (waypoints.length - 1)) * 90 + 5,
    y: (1 - waypoint.z / max) * 100,
  }));
  return (
    <div
      ref={ref}
      style={{
        width: "100%",
        height: "100%",
        position: "relative",
        touchAction: "none",
      }}
    >
      <svg
        width="100%"
        height="100%"
        preserveAspectRatio="none"
        viewBox="0 0 100 100"
      >
        {points.length > 1 && (
          <polyline
            points={points.map((point) => `${point.x} ${point.y}`).join(", ")}
            fill="none"
            stroke="#f59e0b"
            strokeWidth="1"
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>
      {points.map((point, index) => (
        <div
          key={point.id}
          onPointerDown={(event) => drag(point.id, event)}
          style={{
            position: "absolute",
            left: `${point.x}%`,
            top: `${point.y}%`,
            width: 18,
            height: 18,
            backgroundColor: point.id === selectedId ? "#38bdf8" : "#ef4444",
            border:
              point.id === selectedId ? "2px solid white" : "1px solid #7f1d1d",
            borderRadius: "50%",
            transform: "translate(-50%, -50%)",
            cursor: "ns-resize",
            display: "flex",
            justifyContent: "center",
            alignItems: "center",
            fontSize: 10,
            color: "white",
            fontWeight: "bold",
          }}
        >
          {index + 1}
        </div>
      ))}
    </div>
  );
}
