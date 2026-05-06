import { useCallback, useEffect, useState } from "react";
import { getAvailableMachines } from "../services/api";

const EMPTY_SNAPSHOT = {
  community: [],
  vast: [],
  modal: [],
  serverless_in_flight: {},
};

function fmtPrice(v) {
  if (typeof v !== "number" || v <= 0) return null;
  return `$${v.toFixed(3)}/hr`;
}

function fmtSpeed(v) {
  if (typeof v !== "number" || v <= 0) return null;
  return `${v.toFixed(2)}x`;
}

function CommunityCard({ m }) {
  return (
    <div className="card machine-card" style={{ cursor: "default" }}>
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{m.gpu_model || "Unknown GPU"}</div>
          <span className="status-badge status-done">Desktop Worker</span>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">VRAM</span>
            <span className="info-value">{m.vram_gb} GB</span>
          </div>
          <div className="info-item">
            <span className="info-label">CPU</span>
            <span className="info-value">{m.cpu_cores} cores</span>
          </div>
          <div className="info-item">
            <span className="info-label">RAM</span>
            <span className="info-value">{m.ram_gb} GB</span>
          </div>
          {fmtSpeed(m.render_speed) && (
            <div className="info-item">
              <span className="info-label">Speed</span>
              <span className="info-value">{fmtSpeed(m.render_speed)}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function VastCard({ c }) {
  const chips = [];
  if (c.cuda_version) chips.push(`CUDA ${c.cuda_version}`);
  if (c.host_os) chips.push(c.host_os);
  return (
    <div className="card machine-card" style={{ cursor: "default" }}>
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{c.label || c.gpu_type}</div>
          <span className="status-badge status-done">Vast.ai</span>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">VRAM</span>
            <span className="info-value">{c.vram_gb} GB</span>
          </div>
          {fmtSpeed(c.render_speed) && (
            <div className="info-item">
              <span className="info-label">Speed</span>
              <span className="info-value">{fmtSpeed(c.render_speed)}</span>
            </div>
          )}
          {fmtPrice(c.price_per_hour) && (
            <div className="info-item">
              <span className="info-label">Price</span>
              <span className="info-value">{fmtPrice(c.price_per_hour)}</span>
            </div>
          )}
        </div>
        {chips.length > 0 && (
          <div className="machine-chips" style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {chips.map((label) => (
              <span key={label} className="job-detail-meta-chip" style={{ fontSize: 11 }}>
                {label}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ModalCard({ c }) {
  return (
    <div className="card machine-card" style={{ cursor: "default" }}>
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{c.label || c.gpu_type}</div>
          <span className="status-badge status-done">Modal</span>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">VRAM</span>
            <span className="info-value">{c.vram_gb} GB</span>
          </div>
          {fmtSpeed(c.render_speed) && (
            <div className="info-item">
              <span className="info-label">Speed</span>
              <span className="info-value">{fmtSpeed(c.render_speed)}</span>
            </div>
          )}
          {fmtPrice(c.price_per_hour) && (
            <div className="info-item">
              <span className="info-label">Price</span>
              <span className="info-value">{fmtPrice(c.price_per_hour)}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function FleetSection({ title, count, inFlight, items, emptyMsg, renderCard }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <div className="page-header" style={{ paddingBottom: 8, borderBottom: "1px solid rgba(180, 175, 220, 0.1)", marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        <span className="log-count">{count} available</span>
        {typeof inFlight === "number" && inFlight > 0 && (
          <span className="job-detail-meta-chip" style={{ marginLeft: 8 }}>
            {inFlight} in flight
          </span>
        )}
      </div>
      {items.length === 0 ? (
        <div className="empty-state" style={{ padding: 16, fontSize: 13 }}>
          <p className="muted">{emptyMsg}</p>
        </div>
      ) : (
        <div className="machine-list">
          {items.map(renderCard)}
        </div>
      )}
    </section>
  );
}

export default function AvailableMachinesPage({ backendUrl }) {
  const [snapshot, setSnapshot] = useState(EMPTY_SNAPSHOT);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getAvailableMachines(backendUrl);
      setSnapshot({
        community: Array.isArray(data?.community) ? data.community : [],
        vast: Array.isArray(data?.vast) ? data.vast : [],
        modal: Array.isArray(data?.modal) ? data.modal : [],
        serverless_in_flight: data?.serverless_in_flight || {},
      });
    } catch (err) {
      setError(err?.message || "Failed to load available machines");
      setSnapshot(EMPTY_SNAPSHOT);
    } finally {
      setLoading(false);
    }
  }, [backendUrl]);

  useEffect(() => {
    void load();
  }, [load]);

  const total = snapshot.community.length + snapshot.vast.length + snapshot.modal.length;
  const vastInFlight = snapshot.serverless_in_flight?.vast_serverless;
  const modalInFlight = snapshot.serverless_in_flight?.modal_serverless;

  return (
    <div className="page">
      <div className="page-header">
        <h2>Available Machines</h2>
        <span className="log-count">{total} total</span>
        <div className="page-header-actions" style={{ marginLeft: "auto" }}>
          <button className="btn btn-secondary" onClick={() => { void load(); }} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      {error && <p className="error-text">{error}</p>}

      {loading && total === 0 ? (
        <div className="empty-state">
          <p>Loading available machines...</p>
        </div>
      ) : (
        <>
          <FleetSection
            title="Desktop Workers"
            count={snapshot.community.length}
            items={snapshot.community}
            emptyMsg="No desktop workers online right now."
            renderCard={(m) => <CommunityCard key={m.id} m={m} />}
          />
          <FleetSection
            title="Vast.ai Offers"
            count={snapshot.vast.length}
            inFlight={vastInFlight}
            items={snapshot.vast}
            emptyMsg="No Vast.ai offers available right now."
            renderCard={(c, i) => <VastCard key={c.offer_id ?? `${c.gpu_type}-${i}`} c={c} />}
          />
          <FleetSection
            title="Modal Endpoints"
            count={snapshot.modal.length}
            inFlight={modalInFlight}
            items={snapshot.modal}
            emptyMsg="No Modal endpoints with headroom right now."
            renderCard={(c) => <ModalCard key={c.gpu_type} c={c} />}
          />
        </>
      )}
    </div>
  );
}
