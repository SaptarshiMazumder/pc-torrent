import { useMemo } from "react";
import { MeshStandardMaterial } from "three";

/**
 * Placeholder sword built from primitives so the full scene — lighting, embers,
 * bloom, parallax — is testable before a real model exists. Oriented as a blade
 * driven into the floor: tip down (−y), grip and pommel up, the lower blade
 * glowing hot where it meets the flames.
 *
 * Phase 8 replaces the body of this component with
 * `const { scene } = useGLTF("/models/sword.glb"); return <primitive object={scene} .../>;`
 * Nothing else in the scene needs to change — the rig, lighting and embers stay.
 */
export default function ForgeSwordModel() {
  const steel = useMemo(
    () => new MeshStandardMaterial({ color: "#c9ced6", metalness: 1, roughness: 0.28, envMapIntensity: 1.2 }),
    []
  );
  const hotSteel = useMemo(
    () =>
      new MeshStandardMaterial({
        color: "#8a5038",
        metalness: 1,
        roughness: 0.4,
        emissive: "#ff5a1e",
        emissiveIntensity: 1.4,
      }),
    []
  );
  const brass = useMemo(
    () => new MeshStandardMaterial({ color: "#b8863b", metalness: 1, roughness: 0.35, envMapIntensity: 1 }),
    []
  );
  const leather = useMemo(
    () => new MeshStandardMaterial({ color: "#3a2a20", metalness: 0.1, roughness: 0.8 }),
    []
  );

  return (
    <group position={[0, 0, 0]}>
      {/* upper blade — polished steel */}
      <mesh material={steel} position={[0, 0.3, 0]}>
        <boxGeometry args={[0.16, 0.8, 0.04]} />
      </mesh>
      {/* lower blade — glowing hot, crosses the floor line and continues below */}
      <mesh material={hotSteel} position={[0, -0.4, 0]}>
        <boxGeometry args={[0.16, 1.4, 0.04]} />
      </mesh>
      {/* buried tip */}
      <mesh material={hotSteel} position={[0, -1.25, 0]} rotation={[0, 0, Math.PI]}>
        <coneGeometry args={[0.09, 0.35, 4]} />
      </mesh>
      {/* crossguard */}
      <mesh material={brass} position={[0, 0.75, 0]}>
        <boxGeometry args={[0.62, 0.09, 0.1]} />
      </mesh>
      {/* grip */}
      <mesh material={leather} position={[0, 1.02, 0]}>
        <cylinderGeometry args={[0.045, 0.045, 0.42, 16]} />
      </mesh>
      {/* pommel */}
      <mesh material={brass} position={[0, 1.3, 0]}>
        <sphereGeometry args={[0.075, 20, 20]} />
      </mesh>
    </group>
  );
}
