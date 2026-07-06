import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
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

// `available_seconds` from /machines/available is the planner's
// time-budget input: how long the target stays in the pool.  Modal
// gets a config-driven flat value; Vast gets the bundle's `duration`;
// Community computes commitment_end_at - now() at row-read time.  Null
// means "unbounded / unknown" -- planner skips its time check there.
function fmtAvailability(seconds, t) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "—";
  if (seconds <= 0) return t("availability.expired");
  const totalSec = Math.floor(seconds);
  if (totalSec < 60) return `${totalSec}s`;
  const days = Math.floor(totalSec / 86400);
  const rem = totalSec - days * 86400;
  const hours = Math.floor(rem / 3600);
  const minutes = Math.floor((rem - hours * 3600) / 60);
  if (days > 0) return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
  if (hours > 0) return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  return `${minutes}m`;
}

// Warning threshold for "about to expire" styling.  15min matches the
// minimum the desktop's commitment picker accepts -- below that, the
// machine is about to fall off the planner's eligible list.
const AVAILABILITY_WARN_SECONDS = 15 * 60;

function availabilityClass(seconds) {
  if (typeof seconds !== "number" || !Number.isFinite(seconds)) return "";
  if (seconds <= 0) return "info-value--danger";
  if (seconds < AVAILABILITY_WARN_SECONDS) return "info-value--warn";
  return "";
}

function CommunityCard({ m }) {
  const { t } = useTranslation(["availableMachines", "common"]);
  return (
    <div className="card machine-card" style={{ cursor: "default" }}>
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{m.gpu_model || "Unknown GPU"}</div>
          <span className="status-badge status-done">{t("badge.desktopWorker")}</span>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">{t("label.vram")}</span>
            <span className="info-value">{m.vram_gb} GB</span>
          </div>
          <div className="info-item">
            <span className="info-label">{t("label.cpu")}</span>
            <span className="info-value">{t("value.cores", { count: m.cpu_cores })}</span>
          </div>
          <div className="info-item">
            <span className="info-label">{t("label.ram")}</span>
            <span className="info-value">{m.ram_gb} GB</span>
          </div>
          {fmtSpeed(m.render_speed) && (
            <div className="info-item">
              <span className="info-label">{t("label.speed")}</span>
              <span className="info-value">{fmtSpeed(m.render_speed)}</span>
            </div>
          )}
          <div className="info-item">
            <span className="info-label">{t("label.availableFor")}</span>
            <span className={`info-value ${availabilityClass(m.available_seconds)}`}>
              {fmtAvailability(m.available_seconds, t)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function VastCard({ c }) {
  const { t } = useTranslation(["availableMachines", "common"]);
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
            <span className="info-label">{t("label.vram")}</span>
            <span className="info-value">{c.vram_gb} GB</span>
          </div>
          {fmtSpeed(c.render_speed) && (
            <div className="info-item">
              <span className="info-label">{t("label.speed")}</span>
              <span className="info-value">{fmtSpeed(c.render_speed)}</span>
            </div>
          )}
          {fmtPrice(c.price_per_hour) && (
            <div className="info-item">
              <span className="info-label">{t("label.price")}</span>
              <span className="info-value">{fmtPrice(c.price_per_hour)}</span>
            </div>
          )}
          <div className="info-item">
            <span className="info-label">{t("label.availableFor")}</span>
            <span className={`info-value ${availabilityClass(c.available_seconds)}`}>
              {fmtAvailability(c.available_seconds, t)}
            </span>
          </div>
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
  const { t } = useTranslation(["availableMachines", "common"]);
  return (
    <div className="card machine-card" style={{ cursor: "default" }}>
      <div className="machine-card-body">
        <div className="machine-card-header">
          <div className="machine-gpu-name">{c.label || c.gpu_type}</div>
          <span className="status-badge status-done">Modal</span>
        </div>
        <div className="machine-specs">
          <div className="info-item">
            <span className="info-label">{t("label.vram")}</span>
            <span className="info-value">{c.vram_gb} GB</span>
          </div>
          {fmtSpeed(c.render_speed) && (
            <div className="info-item">
              <span className="info-label">{t("label.speed")}</span>
              <span className="info-value">{fmtSpeed(c.render_speed)}</span>
            </div>
          )}
          {fmtPrice(c.price_per_hour) && (
            <div className="info-item">
              <span className="info-label">{t("label.price")}</span>
              <span className="info-value">{fmtPrice(c.price_per_hour)}</span>
            </div>
          )}
          <div className="info-item">
            <span className="info-label">{t("label.availableFor")}</span>
            <span className={`info-value ${availabilityClass(c.available_seconds)}`}>
              {fmtAvailability(c.available_seconds, t)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function FleetSection({ title, count, inFlight, items, emptyMsg, renderCard }) {
  const { t } = useTranslation(["availableMachines", "common"]);
  return (
    <section style={{ marginBottom: 24 }}>
      <div className="page-header" style={{ paddingBottom: 8, borderBottom: "1px solid var(--hair)", marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>{title}</h3>
        <span className="log-count">{t("available", { count })}</span>
        {typeof inFlight === "number" && inFlight > 0 && (
          <span className="job-detail-meta-chip" style={{ marginLeft: 8 }}>
            {t("inFlight", { count: inFlight })}
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
  const { t } = useTranslation(["availableMachines", "common"]);
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
      setError(err?.message || t("loadError"));
      setSnapshot(EMPTY_SNAPSHOT);
    } finally {
      setLoading(false);
    }
  }, [backendUrl, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const total = snapshot.community.length + snapshot.vast.length + snapshot.modal.length;
  const vastInFlight = snapshot.serverless_in_flight?.vast_serverless;
  const modalInFlight = snapshot.serverless_in_flight?.modal_serverless;

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-header-title">
          <div className="page-eyebrow">{t("common:eyebrow.marketplace")}</div>
          <h2>{t("title")}</h2>
        </div>
        <span className="log-count">{t("total", { count: total })}</span>
        <div className="page-header-actions" style={{ marginLeft: "auto" }}>
          <button className="btn btn-secondary" onClick={() => { void load(); }} disabled={loading}>
            {loading ? t("common:actions.refreshing") : t("common:actions.refresh")}
          </button>
        </div>
      </div>

      {error && <p className="error-text">{error}</p>}

      {loading && total === 0 ? (
        <div className="empty-state">
          <p>{t("loading")}</p>
        </div>
      ) : (
        <>
          <FleetSection
            title={t("section.desktopWorkers")}
            count={snapshot.community.length}
            items={snapshot.community}
            emptyMsg={t("empty.desktop")}
            renderCard={(m) => <CommunityCard key={m.id} m={m} />}
          />
          <FleetSection
            title={t("section.vastOffers")}
            count={snapshot.vast.length}
            inFlight={vastInFlight}
            items={snapshot.vast}
            emptyMsg={t("empty.vast")}
            renderCard={(c, i) => <VastCard key={c.offer_id ?? `${c.gpu_type}-${i}`} c={c} />}
          />
          <FleetSection
            title={t("section.modalEndpoints")}
            count={snapshot.modal.length}
            inFlight={modalInFlight}
            items={snapshot.modal}
            emptyMsg={t("empty.modal")}
            renderCard={(c) => <ModalCard key={c.gpu_type} c={c} />}
          />
        </>
      )}
    </div>
  );
}
