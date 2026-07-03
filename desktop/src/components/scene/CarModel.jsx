import { useMemo } from "react";
import { useGLTF } from "@react-three/drei";
import { Box3, Vector3 } from "three";

useGLTF.preload("/models/lamboCar.glb");

// Normalise the car's longest axis to this many world units. The export ships it
// at ~0.05 units (a parent-empty shrinks it), so we measure and rescale on load.
const TARGET_LENGTH = 4.6;

/**
 * The Lamborghini, loaded from `public/models/lamboCar.glb` and rendered on its
 * own. The export is a few centimetres across, so we measure its bounds, recenter
 * its pivot to the origin, and let a wrapper group scale it up to a real size.
 *
 * The scale goes on a WRAPPER group (never on the model's own root) so we can't
 * clobber an existing root scale and accidentally send it to hundreds of units.
 * Frustum culling is disabled — the extreme authored scale confuses three's cull.
 *
 * Tunables: `position` (car centre), `rotation` (facing), `scale` (size nudge).
 */
export default function CarModel({ scale = 1, rotation = [0, 0, 0], position = [0, 0, 0] }) {
  const { scene } = useGLTF("/models/lamboCar.glb");

  const { node, fit } = useMemo(() => {
    const node = scene.clone(true);
    node.updateMatrixWorld(true);

    const box = new Box3().setFromObject(node);
    const size = new Vector3();
    const center = new Vector3();
    box.getSize(size);
    box.getCenter(center);

    const fit = TARGET_LENGTH / Math.max(size.x, size.y, size.z);
    node.position.sub(center); // move the bbox centre to the origin
    node.traverse((o) => {
      if (o.isMesh) {
        o.frustumCulled = false;
        o.castShadow = true;
        o.receiveShadow = true;
      }
    });
    return { node, fit };
  }, [scene]);

  return (
    <group position={position} rotation={rotation} scale={scale}>
      <group scale={fit}>
        <primitive object={node} />
      </group>
    </group>
  );
}
