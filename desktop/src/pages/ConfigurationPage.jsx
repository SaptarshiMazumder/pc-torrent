import { useCallback, useEffect, useMemo, useState } from "react";
import { getAdminConfig, putAdminConfig } from "../services/api";
import { useError } from "../contexts/ErrorContext";
import SectionCard from "../components/configuration/SectionCard";
import NumberField from "../components/configuration/NumberField";
import RatioSlider from "../components/configuration/RatioSlider";
import ToggleSwitch from "../components/configuration/ToggleSwitch";
import TextField from "../components/configuration/TextField";

// Stable deep-equal for dirty detection.  Config is plain JSON, so JSON
// stringify is enough -- key order is preserved by JS object iteration
// for non-numeric keys.
function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

// Small immutable-update helper: returns a new object with the given
// path patched.  Path is a list of keys.
function setPath(obj, path, value) {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  return { ...obj, [head]: setPath(obj?.[head] ?? {}, rest, value) };
}

export default function ConfigurationPage({ backendUrl }) {
  const [original, setOriginal] = useState(null);
  const [draft, setDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [savedAt, setSavedAt] = useState(null);
  const { showError } = useError();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getAdminConfig(backendUrl);
      const cfg = data?.config || {};
      setOriginal(cfg);
      setDraft(cfg);
    } catch (e) {
      const msg = e?.message || "Failed to load config";
      setError(msg);
      showError({ title: "Couldn't load configuration", message: msg });
    } finally {
      setLoading(false);
    }
  }, [backendUrl, showError]);

  useEffect(() => { void load(); }, [load]);

  const dirty = useMemo(
    () => original != null && draft != null && !deepEqual(original, draft),
    [original, draft],
  );

  const update = useCallback((path, value) => {
    setDraft((prev) => (prev ? setPath(prev, path, value) : prev));
  }, []);

  const handleSave = useCallback(async () => {
    if (!draft || saving) return;
    setSaving(true);
    setError("");
    try {
      await putAdminConfig(backendUrl, draft);
      setOriginal(draft);
      setSavedAt(new Date());
    } catch (e) {
      const msg = e?.message || "Save failed";
      setError(msg);
      showError({
        title: "Couldn't save configuration",
        message: msg,
        detail: e?.body || null,
      });
    } finally {
      setSaving(false);
    }
  }, [backendUrl, draft, saving, showError]);

  const handleReload = useCallback(() => { void load(); }, [load]);

  if (loading && draft == null) {
    return (
      <div className="page">
        <div className="page-header"><h2>Configuration</h2></div>
        <div className="empty-state"><p>Loading…</p></div>
      </div>
    );
  }
  if (!draft) {
    return (
      <div className="page">
        <div className="page-header"><h2>Configuration</h2></div>
        {error && <p className="error-text">{error}</p>}
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page-header">
        <h2>Configuration</h2>
        <span className="log-count">{dirty ? "Unsaved changes" : "Saved"}</span>
      </div>

      <div className="cfg-layout">
        <div className="cfg-form">
          <FrameAllocationSection draft={draft} update={update} />
          <StallSection draft={draft} update={update} />
          <MonitorSection draft={draft} update={update} />
          <ModalSection draft={draft} update={update} />
          <VastSection draft={draft} update={update} />
          <CommunitySection draft={draft} update={update} />
          <OrchestratorSection draft={draft} update={update} />
        </div>

        <aside className="cfg-side">
          <div className="cfg-side-card">
            <div className="cfg-side-row">
              <span className="cfg-side-label">Status</span>
              <span className={`cfg-side-value${dirty ? " dirty" : ""}`}>
                {dirty ? "Unsaved" : "Synced"}
              </span>
            </div>
            {savedAt && (
              <div className="cfg-side-row">
                <span className="cfg-side-label">Last saved</span>
                <span className="cfg-side-value">{savedAt.toLocaleTimeString()}</span>
              </div>
            )}
            {error && <div className="error-text" style={{ marginTop: 8 }}>{error}</div>}
            <div className="cfg-side-actions">
              <button
                type="button"
                className="btn btn-secondary"
                onClick={handleReload}
                disabled={loading || saving}
              >
                {loading ? "Reloading…" : "Reload"}
              </button>
              <button
                type="button"
                className="btn btn-primary"
                onClick={handleSave}
                disabled={!dirty || saving}
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
            <p className="cfg-side-hint">
              Changes take effect on the next planning tick.
              vast_instances / modal_instances are deploy-time only and
              are preserved untouched.
            </p>
          </div>
        </aside>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------

function FrameAllocationSection({ draft, update }) {
  const fa = draft.frame_allocation || {};
  const w = fa.weights || {};
  const vfb = fa.vram_fleet_boost || {};
  const sb = fa.startup_buffer_sec || {};
  const fr = fa.failure_rate || {};
  const rt = fa.render_time || {};
  const fc = rt.factors_cycles || {};
  const fe = rt.factors_eevee || {};
  const ssC = rt.scene_scaling_cycles || {};
  const ssE = rt.scene_scaling_eevee || {};
  const ss = rt.startup_sec || {};
  const set = (key, val) => update(["frame_allocation", key], val);
  const setIn = (sub, key, val) => update(["frame_allocation", sub, key], val);
  const setRT = (key, val) => update(["frame_allocation", "render_time", key], val);
  const setRTIn = (sub, key, val) =>
    update(["frame_allocation", "render_time", sub, key], val);

  return (
    <SectionCard title="Frame allocation" subtitle="planner tuning">
      <SectionCard title="Weights" defaultOpen={true}>
        <div className="cfg-grid">
          <RatioSlider label="speed_weight" value={w.speed_weight}
            onChange={(v) => setIn("weights", "speed_weight", v)} />
          <RatioSlider label="cuda_weight" value={w.cuda_weight}
            onChange={(v) => setIn("weights", "cuda_weight", v)} />
          <RatioSlider label="os_weight" value={w.os_weight}
            onChange={(v) => setIn("weights", "os_weight", v)} />
          <NumberField label="max_targets" value={w.max_targets} step={1}
            onChange={(v) => setIn("weights", "max_targets", v)} />
          <NumberField label="min_frames_per_chunk" value={w.min_frames_per_chunk} step={1}
            onChange={(v) => setIn("weights", "min_frames_per_chunk", v)} />
          <RatioSlider label="fleet_diversification_cap" value={w.fleet_diversification_cap}
            onChange={(v) => setIn("weights", "fleet_diversification_cap", v)} />
          <RatioSlider label="gpu_type_diversification_cap" value={w.gpu_type_diversification_cap}
            onChange={(v) => setIn("weights", "gpu_type_diversification_cap", v)} />
          <RatioSlider label="vram_safety_factor" value={w.vram_safety_factor}
            min={1} max={2} step={0.05} precision={2}
            onChange={(v) => setIn("weights", "vram_safety_factor", v)} />
          <RatioSlider label="startup_amortization_ratio" value={w.startup_amortization_ratio}
            onChange={(v) => setIn("weights", "startup_amortization_ratio", v)} />
          <RatioSlider label="chunk_count_curve" value={w.chunk_count_curve}
            min={0.3} max={3} step={0.1} precision={1}
            onChange={(v) => setIn("weights", "chunk_count_curve", v)} />
          <RatioSlider label="time_safety_factor" value={w.time_safety_factor}
            min={1.0} max={3.0} step={0.05} precision={2}
            onChange={(v) => setIn("weights", "time_safety_factor", v)} />
          <RatioSlider label="time_headroom_falloff" value={w.time_headroom_falloff}
            min={0} max={2.0} step={0.05} precision={2}
            onChange={(v) => setIn("weights", "time_headroom_falloff", v)} />
        </div>
      </SectionCard>

      <SectionCard title="VRAM fleet boost" subtitle="multiplier on machine VRAM during eligibility">
        <div className="cfg-grid">
          <RatioSlider label="vast" value={vfb.vast} min={1} max={5} step={0.1} precision={2}
            onChange={(v) => setIn("vram_fleet_boost", "vast", v)} />
          <RatioSlider label="modal" value={vfb.modal} min={1} max={5} step={0.1} precision={2}
            onChange={(v) => setIn("vram_fleet_boost", "modal", v)} />
          <RatioSlider label="community" value={vfb.community} min={1} max={500} step={0.5} precision={2}
            onChange={(v) => setIn("vram_fleet_boost", "community", v)} />
        </div>
      </SectionCard>

      <SectionCard title="Startup buffer (sec)" subtitle="per-fleet additive on planning startup">
        <div className="cfg-grid">
          <NumberField label="vast" value={sb.vast}
            onChange={(v) => setIn("startup_buffer_sec", "vast", v)} />
          <NumberField label="modal" value={sb.modal}
            onChange={(v) => setIn("startup_buffer_sec", "modal", v)} />
          <NumberField label="community" value={sb.community}
            onChange={(v) => setIn("startup_buffer_sec", "community", v)} />
        </div>
      </SectionCard>

      <SectionCard title="Failure rate" subtitle="per-fleet expected failure prob (0–1)">
        <div className="cfg-grid">
          <RatioSlider label="vast" value={fr.vast}
            onChange={(v) => setIn("failure_rate", "vast", v)} />
          <RatioSlider label="modal" value={fr.modal}
            onChange={(v) => setIn("failure_rate", "modal", v)} />
          <RatioSlider label="community" value={fr.community}
            onChange={(v) => setIn("failure_rate", "community", v)} />
        </div>
      </SectionCard>

      <SectionCard title="Render time calibration" defaultOpen={false}>
        <div className="cfg-grid">
          <NumberField label="baseline_sec_cycles" value={rt.baseline_sec_cycles} step={1}
            onChange={(v) => setRT("baseline_sec_cycles", v)} />
          <NumberField label="baseline_sec_eevee" value={rt.baseline_sec_eevee} step={1}
            onChange={(v) => setRT("baseline_sec_eevee", v)} />
        </div>
        <SectionCard title="Cycles factors" defaultOpen={false}>
          <div className="cfg-grid">
            {Object.keys(fc).map((k) => (
              <RatioSlider key={k} label={k} value={fc[k]} min={0.1} max={4} step={0.05}
                onChange={(v) => setRTIn("factors_cycles", k, v)} />
            ))}
          </div>
        </SectionCard>
        <SectionCard title="EEVEE factors" defaultOpen={false}>
          <div className="cfg-grid">
            {Object.keys(fe).map((k) => (
              <RatioSlider key={k} label={k} value={fe[k]} min={0.1} max={4} step={0.05}
                onChange={(v) => setRTIn("factors_eevee", k, v)} />
            ))}
          </div>
        </SectionCard>
        <SectionCard title="Scene scaling — Cycles"
          subtitle="pixel + sample curves: factor = max(min, (value/baseline)^exponent)"
          defaultOpen={false}>
          <div className="cfg-grid">
            <NumberField label="baseline_pixels" value={ssC.baseline_pixels} step={1}
              onChange={(v) => setRTIn("scene_scaling_cycles", "baseline_pixels", v)} />
            <RatioSlider label="pixel_curve_exponent" value={ssC.pixel_curve_exponent}
              min={0} max={2} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_cycles", "pixel_curve_exponent", v)} />
            <RatioSlider label="min_pixel_factor" value={ssC.min_pixel_factor}
              min={0} max={1} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_cycles", "min_pixel_factor", v)} />
            <NumberField label="baseline_samples" value={ssC.baseline_samples} step={1}
              onChange={(v) => setRTIn("scene_scaling_cycles", "baseline_samples", v)} />
            <RatioSlider label="sample_curve_exponent" value={ssC.sample_curve_exponent}
              min={0} max={2} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_cycles", "sample_curve_exponent", v)} />
            <RatioSlider label="min_sample_factor" value={ssC.min_sample_factor}
              min={0} max={1} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_cycles", "min_sample_factor", v)} />
          </div>
        </SectionCard>
        <SectionCard title="Scene scaling — EEVEE"
          subtitle="EEVEE TAA + raster scale sublinearly; defaults pixel=0.5, sample=0.4"
          defaultOpen={false}>
          <div className="cfg-grid">
            <NumberField label="baseline_pixels" value={ssE.baseline_pixels} step={1}
              onChange={(v) => setRTIn("scene_scaling_eevee", "baseline_pixels", v)} />
            <RatioSlider label="pixel_curve_exponent" value={ssE.pixel_curve_exponent}
              min={0} max={2} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_eevee", "pixel_curve_exponent", v)} />
            <RatioSlider label="min_pixel_factor" value={ssE.min_pixel_factor}
              min={0} max={1} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_eevee", "min_pixel_factor", v)} />
            <NumberField label="baseline_samples" value={ssE.baseline_samples} step={1}
              onChange={(v) => setRTIn("scene_scaling_eevee", "baseline_samples", v)} />
            <RatioSlider label="sample_curve_exponent" value={ssE.sample_curve_exponent}
              min={0} max={2} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_eevee", "sample_curve_exponent", v)} />
            <RatioSlider label="min_sample_factor" value={ssE.min_sample_factor}
              min={0} max={1} step={0.05} precision={2}
              onChange={(v) => setRTIn("scene_scaling_eevee", "min_sample_factor", v)} />
          </div>
        </SectionCard>
        <SectionCard title="Startup sec breakdown" defaultOpen={false}>
          <div className="cfg-grid">
            {Object.keys(ss).map((k) => (
              <NumberField key={k} label={k} value={ss[k]} step={0.5}
                onChange={(v) => setRTIn("startup_sec", k, v)} />
            ))}
          </div>
        </SectionCard>
      </SectionCard>
    </SectionCard>
  );
}

function StallSection({ draft, update }) {
  const s = draft.stall || {};
  const set = (k, v) => update(["stall", k], v);
  return (
    <SectionCard title="Stall watchdog" defaultOpen={false}>
      <div className="cfg-grid">
        <RatioSlider label="cpu_threshold_pct" value={s.cpu_threshold_pct}
          min={0} max={100} step={0.5} precision={1}
          onChange={(v) => set("cpu_threshold_pct", v)} />
        <NumberField label="cpu_window_sec" value={s.cpu_window_sec}
          onChange={(v) => set("cpu_window_sec", v)} />
        <NumberField label="rss_noise_bytes" value={s.rss_noise_bytes}
          onChange={(v) => set("rss_noise_bytes", v)} />
        <NumberField label="download_bytes_stall_sec" value={s.download_bytes_stall_sec}
          onChange={(v) => set("download_bytes_stall_sec", v)} />
        <NumberField label="download_secs_per_gb" value={s.download_secs_per_gb}
          onChange={(v) => set("download_secs_per_gb", v)} />
        <NumberField label="download_phase_min_sec" value={s.download_phase_min_sec}
          onChange={(v) => set("download_phase_min_sec", v)} />
        <NumberField label="download_phase_max_sec" value={s.download_phase_max_sec}
          onChange={(v) => set("download_phase_max_sec", v)} />
        <RatioSlider label="loading_multiplier" value={s.loading_multiplier}
          min={1} max={5} step={0.1} precision={2}
          onChange={(v) => set("loading_multiplier", v)} />
        <NumberField label="loading_phase_min_sec" value={s.loading_phase_min_sec}
          onChange={(v) => set("loading_phase_min_sec", v)} />
        <NumberField label="loading_phase_max_sec" value={s.loading_phase_max_sec}
          onChange={(v) => set("loading_phase_max_sec", v)} />
        <NumberField label="hard_max_chunk_sec" value={s.hard_max_chunk_sec}
          onChange={(v) => set("hard_max_chunk_sec", v)} />
      </div>
    </SectionCard>
  );
}

function MonitorSection({ draft, update }) {
  const m = draft.monitor || {};
  return (
    <SectionCard title="Monitor" defaultOpen={false}>
      <div className="cfg-grid">
        <NumberField label="in_progress_stale_sec" value={m.in_progress_stale_sec}
          onChange={(v) => update(["monitor", "in_progress_stale_sec"], v)} />
      </div>
    </SectionCard>
  );
}

function ModalSection({ draft, update }) {
  const m = draft.modal || {};
  const set = (k, v) => update(["modal", k], v);
  return (
    <SectionCard title="Modal" defaultOpen={false}>
      <div className="cfg-grid">
        <NumberField label="max_parallel" value={m.max_parallel}
          onChange={(v) => set("max_parallel", v)} />
        <NumberField label="per_gpu_max_parallel" value={m.per_gpu_max_parallel}
          onChange={(v) => set("per_gpu_max_parallel", v)} />
        <NumberField label="dispatch_timeout_sec" value={m.dispatch_timeout_sec}
          onChange={(v) => set("dispatch_timeout_sec", v)} />
        <NumberField label="in_queue_timeout_sec" value={m.in_queue_timeout_sec}
          onChange={(v) => set("in_queue_timeout_sec", v)} />
        <TextField label="endpoint_url_prefix" value={m.endpoint_url_prefix}
          onChange={(v) => set("endpoint_url_prefix", v)} />
        <ToggleSwitch label="provisioning_enabled" value={m.provisioning_enabled}
          onChange={(v) => set("provisioning_enabled", v)} />
        <NumberField label="availability_sec" value={m.availability_sec} step={60}
          onChange={(v) => set("availability_sec", v)} />
      </div>
    </SectionCard>
  );
}

function VastSection({ draft, update }) {
  const v = draft.vast || {};
  const set = (k, val) => update(["vast", k], val);
  return (
    <SectionCard title="Vast" defaultOpen={false}>
      <div className="cfg-grid">
        <NumberField label="max_parallel" value={v.max_parallel} onChange={(x) => set("max_parallel", x)} />
        <NumberField label="disk_gb" value={v.disk_gb} onChange={(x) => set("disk_gb", x)} />
        <ToggleSwitch label="secure_cloud_only" value={v.secure_cloud_only}
          onChange={(x) => set("secure_cloud_only", x)} />
        <NumberField label="poll_interval_sec" value={v.poll_interval_sec}
          onChange={(x) => set("poll_interval_sec", x)} />
        <NumberField label="startup_timeout_sec" value={v.startup_timeout_sec}
          onChange={(x) => set("startup_timeout_sec", x)} />
        <NumberField label="heartbeat_timeout_sec" value={v.heartbeat_timeout_sec}
          onChange={(x) => set("heartbeat_timeout_sec", x)} />
        <NumberField label="heartbeat_grace_sec" value={v.heartbeat_grace_sec}
          onChange={(x) => set("heartbeat_grace_sec", x)} />
        <ToggleSwitch label="provisioning_enabled" value={v.provisioning_enabled}
          onChange={(x) => set("provisioning_enabled", x)} />
      </div>
    </SectionCard>
  );
}

function CommunitySection({ draft, update }) {
  const c = draft.community || {};
  return (
    <SectionCard title="Community" defaultOpen={false}>
      <div className="cfg-grid">
        <NumberField label="price_per_hour" value={c.price_per_hour} step={0.01}
          onChange={(v) => update(["community", "price_per_hour"], v)} />
        <NumberField label="dispatch_claim_timeout_sec" value={c.dispatch_claim_timeout_sec}
          onChange={(v) => update(["community", "dispatch_claim_timeout_sec"], v)} />
      </div>
    </SectionCard>
  );
}

function OrchestratorSection({ draft, update }) {
  const o = draft.orchestrator || {};
  return (
    <SectionCard title="Orchestrator" defaultOpen={false}>
      <div className="cfg-grid">
        <NumberField label="max_retries" value={o.max_retries} step={1}
          onChange={(v) => update(["orchestrator", "max_retries"], v)} />
      </div>
    </SectionCard>
  );
}
