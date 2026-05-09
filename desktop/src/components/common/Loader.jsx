// Single shared loading indicator -- pure-CSS swapping-squares spinner.
// Drives every "something is loading" affordance in the app so the
// aesthetic stays uniform.  Sizes are coarse on purpose: pick "md" for
// page/panel-level loaders and "sm" for inline (button text, section
// headers, list rows).  Add another size only if a real use case comes
// up; don't proliferate.
export default function Loader({ size = "md", className = "" }) {
  const sizeClass = size === "sm" ? " swapping-squares-spinner--sm" : "";
  const extra = className ? ` ${className}` : "";
  return (
    <div
      className={`swapping-squares-spinner${sizeClass}${extra}`}
      role="status"
      aria-label="Loading"
    >
      <div className="square" />
      <div className="square" />
      <div className="square" />
      <div className="square" />
    </div>
  );
}
