// Scene heaviness panel — shared between Create Render (editable resolution)
// and Job Details (read-only).  Renders the analyzer's per-scene cost signals
// so users can see why a render is sized the way it is, and tweak resolution
// before submission.

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

function ResolutionRow({ heaviness, override, onChange }) {
  const sceneX = heaviness.resolution_x ?? null;
  const sceneY = heaviness.resolution_y ?? null;
  const scenePct = heaviness.resolution_percentage ?? null;

  if (!onChange) {
    const x = override?.resolution_x ?? sceneX;
    const y = override?.resolution_y ?? sceneY;
    const pct = override?.resolution_percentage ?? scenePct;
    const value = x && y
      ? `${x} × ${y}${pct && pct !== 100 ? ` @ ${pct}%` : ""}`
      : "—";
    return <Row label="Resolution" value={value} />;
  }

  const xVal = override?.resolution_x ?? sceneX ?? "";
  const yVal = override?.resolution_y ?? sceneY ?? "";
  const pctVal = override?.resolution_percentage ?? scenePct ?? "";

  const num = (raw) => {
    if (raw === "" || raw == null) return null;
    const n = Number(raw);
    return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
  };

  return (
    <div className="hp-row hp-row-edit">
      <span className="hp-row-label">Resolution</span>
      <div className="hp-resolution-edit">
        <input
          type="number" min="1" placeholder={String(sceneX ?? 1920)}
          value={xVal}
          onChange={(e) => onChange({
            resolution_x: num(e.target.value),
            resolution_y: num(yVal),
            resolution_percentage: num(pctVal),
          })}
          className="hp-input hp-input-num"
        />
        <span className="hp-times">×</span>
        <input
          type="number" min="1" placeholder={String(sceneY ?? 1080)}
          value={yVal}
          onChange={(e) => onChange({
            resolution_x: num(xVal),
            resolution_y: num(e.target.value),
            resolution_percentage: num(pctVal),
          })}
          className="hp-input hp-input-num"
        />
        <span className="hp-at">@</span>
        <input
          type="number" min="1" max="1000" placeholder={String(scenePct ?? 100)}
          value={pctVal}
          onChange={(e) => onChange({
            resolution_x: num(xVal),
            resolution_y: num(yVal),
            resolution_percentage: num(e.target.value),
          })}
          className="hp-input hp-input-pct"
        />
        <span className="hp-pct">%</span>
      </div>
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

export default function HeavinessPanel({
  heaviness,
  loading = false,
  onResolutionChange = null,
  overrides = null,
}) {
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

  return (
    <div className="jd-card hp-panel">
      <div className="jd-card-label">Scene Details</div>

      <Section title="Render">
        <Row label="Engine" value={heaviness.render_engine || "—"} />
        <ResolutionRow
          heaviness={heaviness}
          override={overrides}
          onChange={onResolutionChange}
        />
        <Row
          label="Effective pixels"
          value={eMp ? `${eMp} MP` : "—"}
        />
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
