import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { open } from "@tauri-apps/plugin-shell";
import { useDownloads } from "../contexts/DownloadContext";

function fmtDuration(ms) {
  if (!ms || ms < 0) return "";
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const s = sec % 60;
  if (min < 60) return `${min}m ${s}s`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function DownloadEntry({ dl, onRemove }) {
  const { t } = useTranslation("downloads");
  const pct = dl.totalFiles > 0 ? Math.round((dl.completedFiles / dl.totalFiles) * 100) : 0;
  const isActive = dl.status === "loading";
  const isDone = dl.status === "done";
  const isError = dl.status === "error";
  const elapsed = isActive
    ? Date.now() - dl.startedAt
    : dl.completedAt ? dl.completedAt - dl.startedAt : 0;

  const summaryParts = [];
  if (dl.downloaded > 0) summaryParts.push(t("summary.downloaded", { count: dl.downloaded }));
  if (dl.skipped > 0) summaryParts.push(t("summary.skipped", { count: dl.skipped }));
  if (dl.failed > 0) summaryParts.push(t("summary.failed", { count: dl.failed }));
  const summary = summaryParts.join(t("summarySeparator"));

  const statusColor = isDone ? "#12a150" : isError ? "#ef4444" : "var(--th)";

  return (
    <div className={`dl-entry${isDone ? " dl-entry--done" : ""}${isError ? " dl-entry--error" : ""}`}>
      <div className="dl-entry-top">
        <div className="dl-entry-icon" style={{ color: statusColor }}>
          {isActive ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
            </svg>
          ) : isDone ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" /><path d="M22 4L12 14.01l-3-3" />
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
            </svg>
          )}
        </div>
        <div className="dl-entry-info">
          <span className="dl-entry-name">{dl.jobName}</span>
          <span className="dl-entry-meta">
            {isActive && dl.totalFiles > 0 && (
              <span>{t("filesProgress", { completed: dl.completedFiles, total: dl.totalFiles })}</span>
            )}
            {isDone && summary && <span>{summary}</span>}
            {isError && <span className="dl-entry-err-text">{dl.error || t("status.failed")}</span>}
            {elapsed > 0 && <span className="dl-entry-elapsed">{fmtDuration(elapsed)}</span>}
            {dl.startedAt && <span className="dl-entry-time">{fmtTime(dl.startedAt)}</span>}
          </span>
        </div>
        {!isActive && (
          <button className="dl-entry-dismiss" onClick={onRemove} title={t("removeFromList")}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 6L6 18M6 6l12 12" /></svg>
          </button>
        )}
      </div>

      {isActive && dl.totalFiles > 0 && (
        <div className="dl-entry-progress">
          <div className="dl-entry-bar">
            <div className="dl-entry-fill" style={{ width: `${pct}%` }} />
          </div>
          <span className="dl-entry-pct">{pct}%</span>
        </div>
      )}

      {isDone && dl.path && (
        <button
          className="dl-entry-path"
          onClick={() => { open(dl.path).catch(() => {}); }}
          title={t("openInExplorer")}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" /></svg>
          <span>{dl.path}</span>
          <svg className="dl-entry-path-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M7 17L17 7M17 7H7M17 7v10" /></svg>
        </button>
      )}
    </div>
  );
}

export default function DownloadsPage() {
  const { t } = useTranslation(["downloads", "common"]);
  const { downloads, removeDownload, clearCompleted } = useDownloads();

  const sorted = useMemo(() => {
    const list = Object.values(downloads);
    list.sort((a, b) => {
      if (a.status === "loading" && b.status !== "loading") return -1;
      if (b.status === "loading" && a.status !== "loading") return 1;
      return (b.startedAt || 0) - (a.startedAt || 0);
    });
    return list;
  }, [downloads]);

  const activeCount = sorted.filter((d) => d.status === "loading").length;
  const completedCount = sorted.filter((d) => d.status !== "loading").length;

  return (
    <div className="page dl-page">
      <div className="dl-header">
        <div className="dl-header-left">
          <div className="page-header-title">
            <div className="page-eyebrow">{t("common:eyebrow.rentee")}</div>
            <h2>{t("title")}</h2>
          </div>
          {activeCount > 0 && (
            <span className="dl-header-badge">{t("active", { count: activeCount })}</span>
          )}
        </div>
        {completedCount > 0 && (
          <button className="btn btn-secondary dl-clear-btn" onClick={clearCompleted}>
            {t("clearFinished")}
          </button>
        )}
      </div>

      {sorted.length === 0 && (
        <div className="dl-empty">
          <div className="dl-empty-icon">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.2" opacity="0.3">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3" />
            </svg>
          </div>
          <p>{t("empty")}</p>
          <p className="dl-empty-hint">{t("emptyHint")}</p>
        </div>
      )}

      {sorted.length > 0 && (
        <div className="dl-list">
          {sorted.map((dl) => (
            <DownloadEntry key={dl.id} dl={dl} onRemove={() => removeDownload(dl.id)} />
          ))}
        </div>
      )}
    </div>
  );
}
