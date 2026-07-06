// Scene heaviness panel — read-only scene stats from the analyzer.
// Used by:
//   * Create Render — alongside the form, as reference info (no edits here)
//   * Job Details — to show what the running render is actually doing
//
// All editable fields (resolution, samples, etc.) live in the Create Render
// form itself; this panel never owns user input.  The ``overrides`` prop is
// the resolved settings the render actually uses (Job Details: from
// ``resolved_render_settings``; Create Render: the user's in-progress
// overrides).  Falls back to scene defaults when an override is unset.

import { useTranslation } from "react-i18next";

// Heavy-feature flags in display order.  Labels are looked up per-render
// from myJobs:scene.features.<flag> so they follow the active language.
const HEAVY_FEATURE_FLAGS = [
  "uses_subdivision",
  "uses_displacement",
  "uses_particles",
  "uses_geometry_nodes",
  "uses_subsurface_scattering",
  "uses_volumetrics",
];

function formatBytes(bytes) {
  if (typeof bytes !== "number" || bytes <= 0) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function formatNumber(n) {
  if (typeof n !== "number" || !Number.isFinite(n)) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function effectiveMegapixels(x, y, pct) {
  if (typeof x !== "number" || typeof y !== "number" || x <= 0 || y <= 0) return null;
  const p = typeof pct === "number" && pct > 0 ? pct : 100;
  const px = x * y * (p / 100) ** 2;
  if (px <= 0) return null;
  return (px / 1_000_000).toFixed(1);
}

function Section({ title, children }) {
  return (
    <div className="hp-section">
      <div className="hp-section-title">{title}</div>
      <div className="hp-section-grid">{children}</div>
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="hp-row">
      <span className="hp-row-label">{label}</span>
      <span className="hp-row-value">{value ?? "—"}</span>
    </div>
  );
}

function HeavyFeatureChips({ heaviness }) {
  const { t } = useTranslation(["myJobs", "common"]);
  const active = HEAVY_FEATURE_FLAGS.filter((flag) => Boolean(heaviness[flag]));
  if (active.length === 0) {
    return <span className="hp-row-value muted">{t("scene.none")}</span>;
  }
  return (
    <div className="hp-chips">
      {active.map((flag) => (
        <span key={flag} className="hp-chip">{t(`scene.features.${flag}`)}</span>
      ))}
    </div>
  );
}

export default function HeavinessPanel({ heaviness, loading = false, overrides = null }) {
  const { t } = useTranslation(["myJobs", "common"]);
  if (loading && !heaviness) {
    return (
      <div className="jd-card hp-skeleton">
        <div className="hp-skeleton-row" />
        <div className="hp-skeleton-row" />
        <div className="hp-skeleton-row" />
      </div>
    );
  }
  if (!heaviness) {
    return (
      <div className="jd-card hp-empty">
        <span className="muted">{t("scene.unavailable")}</span>
      </div>
    );
  }

  const effX = overrides?.resolution_x ?? heaviness.resolution_x;
  const effY = overrides?.resolution_y ?? heaviness.resolution_y;
  const effPct = overrides?.resolution_percentage ?? heaviness.resolution_percentage;
  const eMp = effectiveMegapixels(effX, effY, effPct);
  const effSamples = overrides?.cycles_samples ?? heaviness.samples;
  const resolutionLabel = effX && effY
    ? `${effX} × ${effY}${effPct && effPct !== 100 ? ` @ ${effPct}%` : ""}`
    : null;

  return (
    <div className="jd-card hp-panel">
      <div className="jd-card-label">{t("scene.title")}</div>

      <Section title={t("scene.sections.render")}>
        <Row label={t("scene.rows.engine")} value={heaviness.render_engine || "—"} />
        <Row label={t("scene.rows.resolution")} value={resolutionLabel} />
        <Row label={t("scene.rows.effectivePixels")} value={eMp ? `${eMp} MP` : "—"} />
        <Row label={t("scene.rows.samples")} value={formatNumber(effSamples)} />
      </Section>

      <Section title={t("scene.sections.geometry")}>
        <Row label={t("scene.rows.vertices")} value={formatNumber(heaviness.vertex_count_total)} />
        <Row label={t("scene.rows.objects")} value={formatNumber(heaviness.object_count)} />
        <Row label={t("scene.rows.meshes")} value={formatNumber(heaviness.mesh_count)} />
      </Section>

      <Section title={t("scene.sections.assets")}>
        <Row label={t("scene.rows.materials")} value={formatNumber(heaviness.material_count)} />
        <Row label={t("scene.rows.textures")} value={formatNumber(heaviness.texture_count)} />
        <Row label={t("scene.rows.textureSize")} value={formatBytes(heaviness.texture_total_bytes)} />
        <Row label={t("scene.rows.shaderNodes")} value={formatNumber(heaviness.shader_node_count_total)} />
      </Section>

      <Section title={t("scene.sections.heavyFeatures")}>
        <div className="hp-row hp-row-features">
          <HeavyFeatureChips heaviness={heaviness} />
        </div>
      </Section>

      {typeof heaviness.file_size_bytes === "number" && heaviness.file_size_bytes > 0 && (
        <Section title={t("scene.sections.file")}>
          <Row label={t("scene.rows.blendSize")} value={formatBytes(heaviness.file_size_bytes)} />
        </Section>
      )}
    </div>
  );
}
