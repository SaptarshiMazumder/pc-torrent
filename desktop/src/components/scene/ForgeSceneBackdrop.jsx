import { Suspense, useEffect, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { AdaptiveDpr } from "@react-three/drei";
import { EffectComposer, Bloom, Vignette } from "@react-three/postprocessing";
import { ACESFilmicToneMapping } from "three";
import CarModel from "./CarModel";
import GarageModel from "./GarageModel";
import CinematicCamera from "./CinematicCamera";

// Two intensity presets. Hero is the vivid login "main menu" moment; ambient is
// the dimmed, quieter backdrop shown behind the app once signed in.
const PRESETS = {
  hero: { bloom: 1.0, dprMax: 2, vignette: 0.7 },
  ambient: { bloom: 0.7, dprMax: 1.5, vignette: 0.9 },
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

// Tone-mapping exposure is imperative renderer state, not a declarative prop —
// set it in an effect so live edits actually re-apply (unlike `onCreated`, which
// only runs once when the Canvas is created). (Environment intensity is set on
// the <Environment> itself so it isn't reset when the HDRI finishes loading.)
function ToneMapping({ exposure }) {
  const gl = useThree((s) => s.gl);
  useEffect(() => {
    gl.toneMapping = ACESFilmicToneMapping;
    gl.toneMappingExposure = exposure;
  }, [gl, exposure]);
  return null;
}

/**
 * Full-window cinematic backdrop mounted once at the app root and kept alive
 * across login → app so the WebGL context is created a single time. Purely
 * presentational: the only input is `mode` (derived from auth state). Sits behind
 * everything with pointer-events disabled so it never intercepts UI clicks.
 */
export default function ForgeSceneBackdrop({ mode = "hero", cameraSet = "default" }) {
  const preset = PRESETS[mode] ?? PRESETS.hero;
  const reducedMotion = usePrefersReducedMotion();
  const frameloop = useVisibilityFrameloop(reducedMotion);

  return (
    <div className="forge-scene-backdrop" aria-hidden="true">
      <Canvas
        shadows
        frameloop={frameloop}
        dpr={[1, preset.dprMax]}
        camera={{ position: [5, 2, 6.5], fov: 35 }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
      >
        <color attach="background" args={["#0d0f14"]} />
        <ToneMapping exposure={0.6} />

        {/* page-driven: anim 1 (front-left↔left-rear) by default, anim 2
            (right-rear↔front-right) on the create page; smooth cross-fade between */}
        <CinematicCamera set={cameraSet} enabled={!reducedMotion} />

        <Suspense fallback={null}>
          <CarModel position={[0, 0.5, 0]} />
          {/* garage floor: self-centered under the car. scale = tile size vs car,
              position Y = floor height (0 ≈ wheels). Tune both. */}
          <GarageModel scale={0.7} position={[0, 1.1, 0]} />

          {/* no HDRI — car lit only by a low key + faint cool rim light */}
          <ambientLight intensity={0.1} />
          <directionalLight position={[6, 8, 6]} intensity={0.6} />
          <directionalLight position={[-6, 5, -4]} intensity={0.3} color="#8fb2ff" />

          {/* overhead spotlight — casts the car's shadow onto the floor */}
          <spotLight
            castShadow
            position={[1.5, 7, 2]}
            angle={0.5}
            penumbra={0.6}
            intensity={60}
            distance={30}
            shadow-mapSize={[2048, 2048]}
            shadow-bias={-0.0002}
            shadow-camera-near={1}
            shadow-camera-far={30}
          />
        </Suspense>

        <EffectComposer disableNormalPass>
          <Bloom mipmapBlur intensity={preset.bloom} luminanceThreshold={0.75} luminanceSmoothing={0.2} />
          <Vignette offset={0.3} darkness={preset.vignette} />
        </EffectComposer>

        <AdaptiveDpr pixelated={false} />
      </Canvas>

      <div className={`forge-scene-scrim is-${mode}`} />
    </div>
  );
}
