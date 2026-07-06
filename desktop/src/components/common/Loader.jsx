// Single shared loading indicator -- pure-CSS swapping-squares spinner.
// Drives every "something is loading" affordance in the app so the
// aesthetic stays uniform.  Sizes are coarse on purpose: pick "md" for
// page/panel-level loaders and "sm" for inline (button text, section
// headers, list rows).  Add another size only if a real use case comes
// up; don't proliferate.
import { useTranslation } from "react-i18next";

export default function Loader({ size = "md", className = "" }) {
  const { t } = useTranslation("dashboard");
  const sizeClass = size === "sm" ? " swapping-squares-spinner--sm" : "";
  const extra = className ? ` ${className}` : "";
  return (
    <div
      className={`swapping-squares-spinner${sizeClass}${extra}`}
      role="status"
      aria-label={t("loading")}
    >
      <div className="square" />
      <div className="square" />
      <div className="square" />
      <div className="square" />
    </div>
  );
}
