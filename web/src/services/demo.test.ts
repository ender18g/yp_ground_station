import { describe, expect, it } from "vitest";
import { createDemoVehicles, demoVehicleSnapshot, handleDemoCommand, stepDemoVehicle, updateDemoVehicleColor } from "./demo";
import { haversineMeters } from "../utils/geo";

describe("hardware-free telemetry and commands", () => {
  it("publishes the same five telemetry topics per vehicle with a bounded trail", () => {
    const vehicles = createDemoVehicles();
    expect(new Set(vehicles.map((vehicle) => vehicle.vehicle_id)).size).toBe(vehicles.length);
    for (let tick = 0; tick < 510; tick++) {
      for (const vehicle of vehicles) {
        const messages = stepDemoVehicle(vehicle, 0.2, tick / 5, vehicles);
        expect(messages.map((message) => message.topic.split("/").pop())).toEqual([
          "heartbeat", "navsatfix", "pose", "battery", "trajectory",
        ]);
      }
    }
    for (const vehicle of vehicles) {
      const snapshot = demoVehicleSnapshot(vehicle);
      expect(snapshot.connected).toBe(true);
      expect(snapshot.history).toHaveLength(500);
      expect(snapshot.position?.latitude).toBeCloseTo(vehicle.lat);
      expect(Object.keys(snapshot.messages)).toHaveLength(5);
    }
  });

  it("advances mission waypoints and lets a new waypoint interrupt the queue", () => {
    const vehicles = createDemoVehicles();
    const vehicle = vehicles[1];
    const first = { latitude: vehicle.lat, longitude: vehicle.lon, altitude: vehicle.alt };
    const second = { ...first, latitude: first.latitude + 0.001 };
    handleDemoCommand(vehicles, vehicle.vehicle_id, { type: "mission_plan", waypoints: [first, second] });
    stepDemoVehicle(vehicle, 0.2, 1, vehicles);
    expect(vehicle.target).toEqual(second);
    const override = { ...first, longitude: first.longitude - 0.001 };
    handleDemoCommand(vehicles, vehicle.vehicle_id, { type: "waypoint", target: override });
    expect(vehicle.target).toEqual(override);
    expect(vehicle.missionWaypoints).toEqual([]);
    expect(vehicle.mode).toBe("waypoint");
  });

  it("keeps the RTB target following the moving mother ship", () => {
    const vehicles = createDemoVehicles();
    const [yp, vehicle] = vehicles;
    handleDemoCommand(vehicles, vehicle.vehicle_id, { type: "rtb" });
    const previousTarget = { ...vehicle.target };
    stepDemoVehicle(yp, 10, 10, vehicles);
    stepDemoVehicle(vehicle, 0.2, 10, vehicles);
    expect(haversineMeters(previousTarget.latitude, previousTarget.longitude, vehicle.target.latitude, vehicle.target.longitude)).toBeGreaterThan(20);
    expect(vehicle.mode).toBe("rtb");
  });

  it("converts ship-relative waypoints and retains chosen marker colors", () => {
    const vehicles = createDemoVehicles();
    const [yp, vehicle] = vehicles;
    handleDemoCommand(vehicles, vehicle.vehicle_id, {
      type: "ship_relative_trajectory", ship_vehicle_id: yp.vehicle_id,
      local_waypoints: [{ x: 0, y: 0, z: 20 }],
    });
    expect(vehicle.target.latitude).toBeCloseTo(yp.lat);
    expect(vehicle.target.longitude).toBeCloseTo(yp.lon);
    expect(vehicle.target.altitude).toBe(yp.alt + 20);
    updateDemoVehicleColor(vehicles, vehicle.vehicle_id, "#123456");
    expect(demoVehicleSnapshot(vehicle)).toMatchObject({ marker_color: "#123456" });
  });
});
