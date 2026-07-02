import { useMemo, useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { useTexture } from "@react-three/drei";
import { AdditiveBlending, RepeatWrapping } from "three";

/**
 * OPTIONAL upgrade layer — NOT mounted in v1. The flickering flame light in
 * ForgeSceneLighting plus the rising embers already read as fire; this adds a
 * visible textured flame once you provide a flipbook sprite sheet at
 * `/textures/flame.png` (a grid of animation frames).
 *
 * It draws two crossed planes at the blade base and advances the sprite-sheet
 * frame over time. To enable: drop in the texture, set COLUMNS/ROWS to match it,
 * and render <ForgeFlame /> inside the scene (outside MouseParallaxRig so it
 * stays anchored to the floor).
 */
const COLUMNS = 6;
const ROWS = 6;
const FPS = 30;

export default function ForgeFlame({ position = [0, -0.2, 0], scale = 1.4 }) {
  const texture = useTexture("/textures/flame.png");
  const matA = useRef();
  const matB = useRef();

  useMemo(() => {
    texture.wrapS = texture.wrapT = RepeatWrapping;
    texture.repeat.set(1 / COLUMNS, 1 / ROWS);
  }, [texture]);

  useFrame((state) => {
    const frame = Math.floor(state.clock.elapsedTime * FPS) % (COLUMNS * ROWS);
    const col = frame % COLUMNS;
    const row = Math.floor(frame / COLUMNS);
    const offsetX = col / COLUMNS;
    const offsetY = 1 - (row + 1) / ROWS;
    for (const mat of [matA.current, matB.current]) {
      if (mat) mat.map.offset.set(offsetX, offsetY);
    }
  });

  return (
    <group position={position} scale={scale}>
      <mesh>
        <planeGeometry args={[1, 1.4]} />
        <meshBasicMaterial
          ref={matA}
          map={texture.clone()}
          transparent
          depthWrite={false}
          blending={AdditiveBlending}
        />
      </mesh>
      <mesh rotation={[0, Math.PI / 2, 0]}>
        <planeGeometry args={[1, 1.4]} />
        <meshBasicMaterial
          ref={matB}
          map={texture.clone()}
          transparent
          depthWrite={false}
          blending={AdditiveBlending}
        />
      </mesh>
    </group>
  );
}
