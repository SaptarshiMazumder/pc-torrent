import { useRef } from "react";
import { useThree, useFrame } from "@react-three/fiber";
import { MathUtils } from "three";

// All shots share the same framing and differ only in orbit angle, so the whole
// camera reduces to a single animated angle around the car.
const TARGET = [0, 0.4, 0];
const RADIUS = 4.2;
const HEIGHT = 1.2;

// Per-page ping-pong ranges. Each set sweeps back and forth between two angles.
const SETS = {
  default: { lo: 0.45, hi: 2.7 }, // anim 1: front-left <-> left-rear
  create: { lo: 3.6, hi: 5.83 }, // anim 2: right-rear <-> front-right
};

const HALF_PERIOD = 45; // seconds for one lo->hi sweep (the tuned speed)
const SMOOTH_TAU = 2.5; // set-change cross-fade easing (larger = smoother/longer)
const TWO_PI = Math.PI * 2;

/**
 * Page-driven cinematic camera. It ping-pongs the orbit angle within the active
 * set using a cosine curve (zero velocity at both ends → smooth turnarounds).
 * When `set` changes (e.g. entering the Create Render page) the target jumps to
 * the other range and the angle is eased toward it along the shortest path, so
 * the switch cross-fades smoothly rather than cutting.
 *
 * Disabled (prefers-reduced-motion) holds the active set's start angle.
 */
export default function CinematicCamera({ set = "default", enabled = true }) {
  const { camera } = useThree();
  const setRef = useRef(set);
  setRef.current = set; // always read the latest set inside the frame loop
  const angle = useRef(SETS[set]?.lo ?? SETS.default.lo);
  const time = useRef({ start: -1, last: -1 });

  useFrame(() => {
    // Self-time off performance.now() (seconds) rather than R3F's clock: under a
    // throttled / manual frameloop that clock can stall, freezing the phase and
    // feeding the smoother garbage deltas (the stuck + jitter). Wall time is stable.
    const now = performance.now() / 1000;
    if (time.current.start < 0) time.current.start = time.current.last = now;
    const elapsed = now - time.current.start;
    const delta = Math.min(now - time.current.last, 0.1);
    time.current.last = now;

    const { lo, hi } = SETS[setRef.current] ?? SETS.default;

    // continuous cosine ping-pong, eased (zero speed) at both endpoints
    const phase = (elapsed / HALF_PERIOD) * Math.PI;
    const target = lo + (hi - lo) * (0.5 - 0.5 * Math.cos(phase));

    if (enabled) {
      // shortest angular path to the target, then ease toward it
      let t = target;
      t -= Math.round((t - angle.current) / TWO_PI) * TWO_PI;
      const k = 1 - Math.exp(-delta / SMOOTH_TAU);
      angle.current = MathUtils.lerp(angle.current, t, k);
    } else {
      angle.current = lo;
    }

    const a = angle.current;
    camera.position.set(TARGET[0] + Math.sin(a) * RADIUS, HEIGHT, TARGET[2] + Math.cos(a) * RADIUS);
    camera.lookAt(TARGET[0], TARGET[1], TARGET[2]);
  });

  return null;
}
