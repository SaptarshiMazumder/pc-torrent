import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { Environment, Lightformer } from "@react-three/drei";

// Per-mode light budget. Hero (login) is dramatic and bright; ambient (in-app)
// is dimmed so it reads as atmosphere behind the UI, not a distraction.
const MODE = {
  hero: { key: 9, rim: 2.2, ambient: 0.25 },
  ambient: { key: 5, rim: 1.2, ambient: 0.18 },
};

/**
 * The scene light rig. A warm point light at the blade base flickers like a
 * flame (layered sines → candle-ish, no randomness needed), a cool directional
 * rim separates the sword from the dark background, and a baked procedural
 * Environment provides image-based reflections so the metal reads as metal.
 *
 * The Environment is built from Lightformers (baked once via `frames={1}`) so
 * it needs no network — important because a packaged Tauri app is offline and
 * drei's CDN presets would silently fail. Phase 8 can swap this for a real
 * HDRI: `<Environment files="/hdri/forge.hdr" />`.
 */
export default function ForgeSceneLighting({ mode = "hero", animate = true }) {
  const flame = useRef();
  const m = MODE[mode] ?? MODE.hero;

  useFrame((state) => {
    if (!animate || !flame.current) return;
    const t = state.clock.elapsedTime;
    const flicker =
      0.75 +
      0.25 * (Math.sin(t * 11) * 0.5 + Math.sin(t * 19 + 1.3) * 0.3 + Math.sin(t * 3.5) * 0.2);
    flame.current.intensity = m.key * flicker;
  });

  return (
    <>
      <ambientLight intensity={m.ambient} color="#3a4256" />
      <pointLight
        ref={flame}
        position={[0, -0.4, 0.6]}
        color="#ff6a2a"
        intensity={m.key}
        distance={9}
        decay={2}
      />
      <directionalLight position={[-4, 5, -3]} color="#6f8bd6" intensity={m.rim} />

      <Environment resolution={256} frames={1}>
        <Lightformer intensity={2.4} color="#ff7a3a" position={[0, -1.5, 1.5]} scale={[8, 8, 1]} />
        <Lightformer intensity={0.7} color="#5ea0fa" position={[-4, 3, -3]} scale={[5, 5, 1]} />
        <Lightformer intensity={0.4} color="#ffffff" position={[3, 2, 2]} scale={[3, 3, 1]} />
      </Environment>
    </>
  );
}
