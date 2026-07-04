import { Suspense, useEffect, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { AdaptiveDpr, Environment, Lightformer, ContactShadows } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import { ACESFilmicToneMapping } from "three";
import CarModel from "./CarModel";
import GarageModel from "./GarageModel";
import CinematicCamera from "./CinematicCamera";

// Two intensity presets. Hero is the vivid login "main menu" moment; ambient is
// the dimmed, quieter backdrop shown behind the app once signed in.
const PRESETS = {
  hero: { dprMax: 2 },
  ambient: { dprMax: 1.75 },
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

// Throttled render loop: caps the backdrop to `fps` frames/sec so the GPU idles
// between frames instead of rendering flat-out (which pins utilisation near 100%
// no matter how cheap each frame is, and can run uncapped in a WebView). Skips
// rendering while the window is hidden. Used with the Canvas in
// `frameloop="never"` so R3F only draws when we call advance().
function ThrottledRenderer({ fps }) {
  const advance = useThree((s) => s.advance);
  useEffect(() => {
    let raf;
    let last = -Infinity;
    const interval = 1000 / fps;
    const loop = (t) => {
      raf = requestAnimationFrame(loop);
      if (document.hidden) return; // pause while minimised / tabbed away
      if (t - last >= interval) {
        last = t;
        advance(t);
      }
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [advance, fps]);
  return null;
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

  return (
    <div className="forge-scene-backdrop" aria-hidden="true">
      <Canvas
        frameloop={reducedMotion ? "demand" : "never"}
        dpr={[1, preset.dprMax]}
        camera={{ position: [5, 2, 6.5], fov: 35 }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
      >
        <color attach="background" args={["#0d0f14"]} />
        <ToneMapping exposure={0.5} />
        {/* cap the frame rate so the GPU idles between frames (see ThrottledRenderer) */}
        {!reducedMotion && <ThrottledRenderer fps={30} />}

        {/* page-driven: anim 1 (front-left↔left-rear) by default, anim 2
            (right-rear↔front-right) on the create page; smooth cross-fade between */}
        <CinematicCamera set={cameraSet} enabled={!reducedMotion} />

        <Suspense fallback={null}>
          <CarModel position={[0, 0.5, 0]} />
          {/* garage floor: self-centered under the car. scale = tile size vs car,
              position Y = floor height (0 ≈ wheels). Tune both. */}
          <GarageModel scale={0.7} position={[0, 1.1, 0]} />

          {/* Studio lighting: procedural soft-box "environment" the paint reflects —
              this is what makes a car read as real (offline-safe, no HDRI file). The
              side strips create the signature reflection streaks along the body. */}
          <Environment resolution={512} frames={1}>
            <color attach="background" args={["#111114"]} />
            <Lightformer intensity={2.5} position={[0, 6, 0]} rotation={[Math.PI / 2, 0, 0]} scale={[10, 10, 1]} />
            <Lightformer intensity={4} position={[-5, 2, 1]} rotation={[0, Math.PI / 2, 0]} scale={[14, 2, 1]} />
            <Lightformer intensity={4} position={[5, 2, 1]} rotation={[0, -Math.PI / 2, 0]} scale={[14, 2, 1]} />
            <Lightformer intensity={2} color="#bcd3ff" position={[-5, 4, -2]} rotation={[0, Math.PI / 2, 0]} scale={[14, 1, 1]} />
            <Lightformer intensity={2} color="#ffe6c2" position={[5, 4, -2]} rotation={[0, -Math.PI / 2, 0]} scale={[14, 1, 1]} />
            <Lightformer intensity={1.5} position={[0, 2, 6]} scale={[8, 4, 1]} />
          </Environment>

          {/* soft contact shadow grounds the car (baked once — nothing moves) */}
          <ContactShadows position={[0, 0.02, 0]} scale={12} blur={2.5} opacity={0.75} far={4} resolution={1024} frames={1} />
          {/* No real-time lights: the car is lit entirely by the baked studio
              environment above — reflections stay, everything else is static. */}
        </Suspense>

        {/* lean bloom — high threshold so ONLY the bright head/tail lights glow.
            Much cheaper than before thanks to the 24fps cap + low DPR. */}
        <EffectComposer disableNormalPass>
          <Bloom mipmapBlur intensity={1.4} luminanceThreshold={0.8} luminanceSmoothing={0.1} radius={0.5} />
        </EffectComposer>

        <AdaptiveDpr pixelated={false} />
      </Canvas>

      <div className={`forge-scene-scrim is-${mode}`} />
      {/* free vignette — a compositor gradient, no GPU render cost (replaces the
          removed post-processing Vignette pass) */}
      <div className="forge-scene-vignette" />
    </div>
  );
}
