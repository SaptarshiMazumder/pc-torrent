import { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { AdditiveBlending } from "three";

// Embers spawn in a disc at the floor line and rise; hero mode is denser and
// faster than the calmer ambient backdrop.
const MODE = {
  hero: { area: 1.1, rise: 0.9, size: 0.05 },
  ambient: { area: 0.9, rise: 0.5, size: 0.035 },
};

const FLOOR_Y = -0.55;
const CEILING_Y = 2.0;

/**
 * A lightweight additive-blended particle system for rising embers. Positions
 * are advanced on the CPU each frame and respawned at the base when they pass
 * the ceiling. Additive blending + the scene's Bloom pass are what make them
 * glow like sparks off a hot blade.
 */
export default function ForgeEmberField({ count = 400, animate = true, mode = "hero" }) {
  const points = useRef();
  const m = MODE[mode] ?? MODE.hero;

  const { positions, seeds } = useMemo(() => {
    const positions = new Float32Array(count * 3);
    const seeds = new Float32Array(count); // per-ember speed + sway phase
    for (let i = 0; i < count; i++) {
      const r = Math.sqrt(Math.random()) * m.area;
      const a = Math.random() * Math.PI * 2;
      positions[i * 3 + 0] = Math.cos(a) * r;
      positions[i * 3 + 1] = FLOOR_Y + Math.random() * (CEILING_Y - FLOOR_Y);
      positions[i * 3 + 2] = Math.sin(a) * r;
      seeds[i] = 0.5 + Math.random();
    }
    return { positions, seeds };
  }, [count, m.area]);

  useFrame((state, delta) => {
    if (!animate || !points.current) return;
    const arr = points.current.geometry.attributes.position.array;
    const t = state.clock.elapsedTime;
    for (let i = 0; i < count; i++) {
      const iy = i * 3 + 1;
      arr[iy] += delta * m.rise * seeds[i];
      arr[i * 3] += Math.sin(t * seeds[i] * 2 + i) * delta * 0.03;
      if (arr[iy] > CEILING_Y) arr[iy] = FLOOR_Y;
    }
    points.current.geometry.attributes.position.needsUpdate = true;
  });

  return (
    <points ref={points}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial
        transparent
        depthWrite={false}
        blending={AdditiveBlending}
        color="#ff8a3a"
        size={m.size}
        sizeAttenuation
        opacity={0.9}
      />
    </points>
  );
}
