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

const HEAVY_FEATURE_LABELS = {
  uses_subdivision: "Subdivision",
  uses_displacement: "Displacement",
  uses_particles: "Particles",
  uses_geometry_nodes: "Geometry Nodes",
  uses_subsurface_scattering: "SSS",
  uses_volumetrics: "Volumetrics",
};

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
  const active = Object.entries(HEAVY_FEATURE_LABELS).filter(
    ([flag]) => Boolean(heaviness[flag])
  );
  if (active.length === 0) {
    return <span className="hp-row-value muted">None</span>;
  }
  return (
    <div className="hp-chips">
      {active.map(([flag, label]) => (
        <span key={flag} className="hp-chip">{label}</span>
      ))}
    </div>
  );
}

export default function HeavinessPanel({ heaviness, loading = false, overrides = null }) {
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
        <span className="muted">Scene details unavailable for this render.</span>
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
      <div className="jd-card-label">Scene Details</div>

      <Section title="Render">
        <Row label="Engine" value={heaviness.render_engine || "—"} />
        <Row label="Resolution" value={resolutionLabel} />
        <Row label="Effective pixels" value={eMp ? `${eMp} MP` : "—"} />
        <Row label="Samples" value={formatNumber(effSamples)} />
      </Section>

      <Section title="Geometry">
        <Row label="Vertices" value={formatNumber(heaviness.vertex_count_total)} />
        <Row label="Objects" value={formatNumber(heaviness.object_count)} />
        <Row label="Meshes" value={formatNumber(heaviness.mesh_count)} />
      </Section>

      <Section title="Assets">
        <Row label="Materials" value={formatNumber(heaviness.material_count)} />
        <Row label="Textures" value={formatNumber(heaviness.texture_count)} />
        <Row label="Texture size" value={formatBytes(heaviness.texture_total_bytes)} />
        <Row label="Shader nodes" value={formatNumber(heaviness.shader_node_count_total)} />
      </Section>

      <Section title="Heavy features">
        <div className="hp-row hp-row-features">
          <HeavyFeatureChips heaviness={heaviness} />
        </div>
      </Section>

      {typeof heaviness.file_size_bytes === "number" && heaviness.file_size_bytes > 0 && (
        <Section title="File">
          <Row label="Blend size" value={formatBytes(heaviness.file_size_bytes)} />
        </Section>
      )}
    </div>
  );
}
