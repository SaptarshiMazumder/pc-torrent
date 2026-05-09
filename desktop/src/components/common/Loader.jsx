// Single shared loading indicator -- pure-CSS bouncing ball on a track.
// Drives every "something is loading" affordance in the app so the
// aesthetic stays uniform.  Sizes are coarse on purpose: pick "md" for
// page/panel-level loaders and "sm" for inline (button text, section
// headers, list rows).  Add another size only if a real use case comes
// up; don't proliferate.
export default function Loader({ size = "md", className = "" }) {
  const sizeClass = size === "sm" ? " loader--sm" : "";
  const extra = className ? ` ${className}` : "";
  return <div className={`loader${sizeClass}${extra}`} role="status" aria-label="Loading" />;
}
