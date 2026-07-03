import { useMemo } from "react";
import { useGLTF } from "@react-three/drei";
import { Box3, Vector3 } from "three";

useGLTF.preload("/models/Garage.glb");

/**
 * The garage floor from `public/models/Garage.glb` — a large concrete plane.
 *
 * We recenter its bounding box to the origin on load, so it stays centered under
 * the car and doesn't drift when scaled. The wrapper group then just sizes it
 * (`scale`) and sets floor height (`position` Y). Because the plane is slightly
 * tilted, its bbox centre ≈ the floor height at the car, so `position` Y near 0
 * lands the floor at the wheels (the car's wheels sit at ~Y 0).
 *
 * Tunables: `scale` (floor/tile size vs the car), `position` Y (floor height),
 * `rotation` (to level it if it reads as sloped).
 */
export default function GarageModel({ position = [0, 0, 0], rotation = [0, 0, 0], scale = 1 }) {
  const { scene } = useGLTF("/models/Garage.glb");

  const model = useMemo(() => {
    const clone = scene.clone(true);
    clone.updateMatrixWorld(true);
    const center = new Vector3();
    new Box3().setFromObject(clone).getCenter(center);
    clone.position.sub(center); // recenter bbox to the origin
    clone.traverse((o) => {
      if (o.isMesh) {
        o.frustumCulled = false;
        o.receiveShadow = true;
      }
    });
    return clone;
  }, [scene]);

  return (
    <group position={position} rotation={rotation} scale={scale}>
      <primitive object={model} />
    </group>
  );
}
