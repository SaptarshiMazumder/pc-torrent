import { Suspense, useEffect, useState } from "react";
import { Canvas } from "@react-three/fiber";
import { AdaptiveDpr } from "@react-three/drei";
import { EffectComposer, Bloom, Vignette } from "@react-three/postprocessing";
import { ACESFilmicToneMapping } from "three";
import ForgeSceneLighting from "./ForgeSceneLighting";
import ForgeSwordModel from "./ForgeSwordModel";
import ForgeEmberField from "./ForgeEmberField";
import MouseParallaxRig from "./MouseParallaxRig";
import { useWindowPointer } from "../../hooks/useWindowPointer";

// Two intensity presets. Hero is the vivid login "main menu" moment; ambient is
// the dimmed, quieter backdrop shown behind the app once signed in.
const PRESETS = {
  hero: { bloom: 1.5, embers: 500, parallax: 0.18, dprMax: 2, vignette: 0.7 },
  ambient: { bloom: 0.9, embers: 180, parallax: 0.08, dprMax: 1.5, vignette: 0.95 },
};

function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduced;
}

// Pause the render loop when the window is hidden/minimised (big GPU + battery
// win while the user works in another window). Reduced-motion renders a single
// static frame instead of animating.
function useVisibilityFrameloop(reducedMotion) {
  const [frameloop, setFrameloop] = useState("always");
  useEffect(() => {
    if (reducedMotion) {
      setFrameloop("demand");
      return;
    }
    const update = () => setFrameloop(document.hidden ? "never" : "always");
    update();
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, [reducedMotion]);
  return frameloop;
}

/**
 * Full-window cinematic backdrop mounted once at the app root and kept alive
 * across login → app so the WebGL context is created a single time. Purely
 * presentational: the only input is `mode` (derived from auth state) and the
 * window pointer. Sits behind everything with pointer-events disabled so it
 * never intercepts UI clicks.
 */
export default function ForgeSceneBackdrop({ mode = "hero" }) {
  const preset = PRESETS[mode] ?? PRESETS.hero;
  const reducedMotion = usePrefersReducedMotion();
  const frameloop = useVisibilityFrameloop(reducedMotion);
  const pointer = useWindowPointer();

  return (
    <div className="forge-scene-backdrop" aria-hidden="true">
      <Canvas
        frameloop={frameloop}
        dpr={[1, preset.dprMax]}
        camera={{ position: [0, 0.6, 4.2], fov: 40 }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
        onCreated={({ gl, camera }) => {
          gl.toneMapping = ACESFilmicToneMapping;
          gl.toneMappingExposure = 1.1;
          camera.lookAt(0, 0.2, 0);
        }}
      >
        <color attach="background" args={["#0d0f14"]} />
        <fog attach="fog" args={["#0d0f14", 6, 16]} />

        <Suspense fallback={null}>
          <ForgeSceneLighting mode={mode} animate={!reducedMotion} />

          {/* dark, faintly glossy floor — catches the flame glow + reflections */}
          <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.55, 0]}>
            <planeGeometry args={[40, 40]} />
            <meshStandardMaterial color="#0a0b0f" metalness={0.6} roughness={0.5} envMapIntensity={0.5} />
          </mesh>

          <MouseParallaxRig pointer={pointer} strength={preset.parallax} interactive={!reducedMotion}>
            <ForgeSwordModel />
            <ForgeEmberField count={preset.embers} animate={!reducedMotion} mode={mode} />
          </MouseParallaxRig>
        </Suspense>

        <EffectComposer disableNormalPass>
          <Bloom mipmapBlur intensity={preset.bloom} luminanceThreshold={0.55} luminanceSmoothing={0.2} />
          <Vignette offset={0.28} darkness={preset.vignette} />
        </EffectComposer>

        <AdaptiveDpr pixelated={false} />
      </Canvas>

      <div className={`forge-scene-scrim is-${mode}`} />
    </div>
  );
}
