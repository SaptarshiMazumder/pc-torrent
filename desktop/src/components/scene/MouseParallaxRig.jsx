import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { MathUtils } from "three";

/**
 * Wraps the scene's hero geometry and gently rotates it toward the pointer.
 * The rotation is damped (`lerp`) every frame — that smoothing is what gives
 * the "expensive AAA menu" feel rather than a rigid 1:1 follow.
 *
 * `pointer` is the ref from useWindowPointer. `interactive` gates the whole
 * effect (disabled for prefers-reduced-motion), and `strength` scales how far
 * the rig turns (larger on the login hero, smaller as an ambient backdrop).
 */
export default function MouseParallaxRig({ children, pointer, strength = 0.15, interactive = true }) {
  const group = useRef();

  useFrame(() => {
    if (!group.current) return;
    const p = pointer.current;
    const targetY = interactive ? p.x * strength * Math.PI : 0;
    const targetX = interactive ? -p.y * strength * Math.PI * 0.4 : 0;
    group.current.rotation.y = MathUtils.lerp(group.current.rotation.y, targetY, 0.045);
    group.current.rotation.x = MathUtils.lerp(group.current.rotation.x, targetX, 0.045);
  });

  return <group ref={group}>{children}</group>;
}
