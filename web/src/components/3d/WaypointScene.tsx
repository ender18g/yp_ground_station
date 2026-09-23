import { Suspense } from "react";
import { Canvas } from "@react-three/fiber";
import { Line, OrbitControls, Sphere, useGLTF } from "@react-three/drei";

interface SceneWaypoint {
  id: string;
  x: number;
  y: number;
  z: number;
  yaw?: number;
}

function ShipModel({ modelUrl }: { modelUrl: string }) {
  const { scene } = useGLTF(modelUrl);
  return <primitive object={scene} scale={0.01} rotation={[-Math.PI / 2, 0, 0]} position={[0, -2, 5]} />;
}

const SHIP_MODEL_URL = `${import.meta.env.BASE_URL}logos/YP_CAD.glb`;

export function WaypointScene({ waypoints, selectedId }: { waypoints: SceneWaypoint[]; selectedId: string | null }) {
  const linePoints = waypoints.map((waypoint) => [-waypoint.x, waypoint.z, waypoint.y] as [number, number, number]);
  return (
    <Canvas camera={{ position: [60, 50, 60], fov: 45 }}>
      <ambientLight intensity={0.5} />
      <directionalLight position={[10, 10, 5]} intensity={1.5} />
      <Suspense fallback={<mesh position={[0, 0, 0]}><boxGeometry args={[2, 2, 2]} /><meshStandardMaterial color="#f97316" wireframe /></mesh>}>
        <ShipModel modelUrl={SHIP_MODEL_URL} />
      </Suspense>
      {waypoints.map((waypoint) => (
        <Sphere key={waypoint.id} position={[-waypoint.x, waypoint.z, waypoint.y]} args={[1.5, 16, 16]}>
          <meshStandardMaterial
            color={waypoint.id === selectedId ? "#38bdf8" : "#ef4444"}
            emissive={waypoint.id === selectedId ? "#38bdf8" : "#ef4444"}
            emissiveIntensity={0.6}
          />
        </Sphere>
      ))}
      {waypoints.map((waypoint) => (
        // Cone points along ship-relative yaw (0deg = ship's bow), matching the 2D planner's convention.
        <mesh
          key={`${waypoint.id}-yaw`}
          position={[-waypoint.x, waypoint.z, waypoint.y]}
          rotation={[Math.PI / 2, -((waypoint.yaw ?? 0) * Math.PI) / 180, 0]}
        >
          <coneGeometry args={[0.8, 3, 12]} />
          <meshStandardMaterial color={waypoint.id === selectedId ? "#38bdf8" : "#f59e0b"} />
        </mesh>
      ))}
      {linePoints.length > 1 && <Line points={linePoints} color="#f59e0b" lineWidth={5} />}
      <OrbitControls makeDefault target={[0, 0, 0]} />
      <gridHelper args={[150, 150, "#334155", "#1e293b"]} position={[0, -2, 0]} />
    </Canvas>
  );
}
