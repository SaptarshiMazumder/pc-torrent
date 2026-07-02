import { useEffect, useRef } from "react";

/**
 * Tracks the pointer position across the whole window, normalised to [-1, 1]
 * on each axis with the origin at the centre and +y pointing up.
 *
 * Returns a stable ref whose `.current` is mutated in place, so consumers can
 * read it inside a render loop (`useFrame`) without triggering React re-renders.
 * Listening at the window level lets the scene canvas keep `pointer-events: none`
 * so it never steals clicks meant for the app UI on top of it.
 */
export function useWindowPointer() {
  const pointer = useRef({ x: 0, y: 0 });

  useEffect(() => {
    function handleMove(event) {
      pointer.current.x = (event.clientX / window.innerWidth) * 2 - 1;
      pointer.current.y = -((event.clientY / window.innerHeight) * 2 - 1);
    }
    window.addEventListener("pointermove", handleMove, { passive: true });
    return () => window.removeEventListener("pointermove", handleMove);
  }, []);

  return pointer;
}
