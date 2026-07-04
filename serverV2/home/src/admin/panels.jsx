// Admin panels — each backs one tab and polls its /admin/* endpoint.

import { useEffect, useRef, useState } from "react";
import { api, sseUrl } from "../api.js";
import {
  fmtAgo, fmtClock, fmtCredits, fmtDay, fmtHours, fmtNum, fmtSecs, fmtUsd, shortId,
} from "../format.js";
import {
  BarChart, DataTable, Pill, Progress, RefreshBar, Tile, statusTone, usePoll,
} from "../components/ui.jsx";

// Slow-poll cadence for non-live panels (costs, GCP, users, history, config):
// they refresh every 30 min and expose a manual Refresh button, instead of
// hammering the backend / external APIs every few seconds.  Live panels
// (overview, live renders/jobs/instances, Redis stream, logs) keep their fast
// intervals.
const SLOW_MS = 30 * 60 * 1000;

// Hard floor for ALL polling in the dashboard: 60s.  Nothing polls faster than
// this — "live" panels use LIVE_MS, non-live use SLOW_MS.  (Logs is push/SSE,
// not polling, and reads an in-memory ring, so it's exempt.)
const LIVE_MS = 60 * 1000;

// Human identity for a row that carries user_email / user_display_name
// (annotated server-side), falling back to the short uid.
function userLabel(row) {
  return row.user_email || row.user_display_name || shortId(row.user_id);
}

// Static descriptions for the daemon-health table (cadence in seconds is used
// to decide when a heartbeat counts as "stale").
const DAEMON_INFO = {
  vast_monitor: {
    label: "Vast monitor",
    desc: "Scans active Vast render jobs; detects stalls, failures & completion.",
    cadence: 10,
  },
  modal_monitor: {
    label: "Modal monitor",
    desc: "Scans active Modal containers; detects stalls, failures & completion.",
    cadence: 10,
  },
  community_monitor: {
    label: "Community monitor",
    desc: "Scans community jobs; offline & stall detection, prunes machine liveness.",
    cadence: 10,
  },
  dispatch_daemon: {
    label: "Dispatch daemon",
    desc: "Drains the dispatch queue to fleets and plans pending allocations.",
    cadence: 10,
  },
};

function daemonHealth(d) {
  if (!d.reporting) return { tone: "critical", text: "no heartbeat" };
  if (d.error) return { tone: "critical", text: "error" };
  const cadence = DAEMON_INFO[d.name]?.cadence || 10;
  if (d.age_sec != null && d.age_sec > cadence * 4) {
    return { tone: "warning", text: `stale · ${fmtSecs(d.age_sec)}` };
  }
  return { tone: "good", text: `ticking · ${fmtSecs(d.age_sec)} ago` };
}

// Render a daemon's per-tick detail dict as "k: v · k: v".
function daemonDetail(detail) {
  const entries = Object.entries(detail || {});
  if (entries.length === 0) return "–";
  return entries
    .map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(",") : v ?? "–"}`)
    .join("  ·  ");
}

function PanelHead({ title, sub, children }) {
  return (
    <div className="panel-head">
      <h2>{title}</h2>
      {sub ? <span className="sub">{sub}</span> : null}
      <span className="spacer" />
      {children}
    </div>
  );
}

function ErrorBanner({ error }) {
  if (!error) return null;
  return <div className="error-banner">Request failed: {String(error.message || error)}</div>;
}

// ---------------------------------------------------------------- overview

function CostStrip() {
  const { data } = usePoll(() => api("/admin/costs/overview"), SLOW_MS, [], "admin:costs:overview");
  if (!data) return null;
  const top = (data.sources || [])[0];
  return (
    <div className="section">
      <h3>
        Cost <span className="sub">· this month + live burn · </span>
        <button className="back-link" onClick={() => { window.location.hash = "/costs"; }}>full breakdown →</button>
      </h3>
      <div className="cost-strip">
        <div className="cs-tile"><div className="label">This month · all sources</div><div className="value">{fmtUsd(data.total_month_to_date_usd)}</div></div>
        <div className="cs-tile"><div className="label">Live burn</div><div className="value">{fmtUsd(data.total_live_rate_usd_per_hr)}/hr</div></div>
        {top ? (
          <div className="cs-tile"><div className="label">Top: {top.label}</div><div className="value">{fmtUsd(top.month_to_date_usd ?? top.projected_monthly_usd ?? 0)}</div></div>
        ) : null}
      </div>
    </div>
  );
}

function DaemonDetail({ name, onClose }) {
  const { data, error } = usePoll(() => api(`/admin/daemons/${name}`), LIVE_MS, [name], `admin:daemon:${name}`);
  const current = data?.current;
  const tone = !current ? "neutral" : current.error ? "critical" : "good";
  const text = !current ? "no heartbeat" : current.error ? "error" : "healthy";
  const activity = (data?.series || [])
    .filter((s) => s.ticks != null)
    .map((s) => ({ label: fmtClock(s.ts), value: s.ticks }));
  const metricKeys = data?.metric_keys || [];
  const metricSeries = data?.metric_series || {};
  return (
    <>
      <div className="panel-head">
        <button className="back-link" onClick={onClose}>← overview</button>
        <h2 style={{ fontSize: 15, margin: 0 }}>
          {DAEMON_INFO[name]?.label || name} <Pill tone={tone}>{text}</Pill>
        </h2>
      </div>
      <ErrorBanner error={error} />
      {DAEMON_INFO[name]?.desc ? <div className="hint" style={{ marginBottom: 8 }}>{DAEMON_INFO[name].desc}</div> : null}
      <div className="tiles">
        <Tile label="Owner instance" value={<span className="mono">{shortId(current?.owner)}</span>} />
        <Tile label="Ticks (total)" value={fmtNum(current?.tick_count)} />
        <Tile label="History samples" value={fmtNum(data?.samples)} />
        <Tile label="Errors in history" value={fmtNum(data?.error_count)} />
      </div>
      {current?.error ? <div className="error-banner">Last error: {String(current.error).slice(0, 200)}</div> : null}
      <div className="section">
        <div className="card chart-card">
          <div className="chart-title">Activity — ticks per ~15s sample</div>
          <BarChart data={activity} formatValue={(v) => `${fmtNum(v)} ticks`} />
        </div>
      </div>
      {metricKeys.map((k) => {
        const pts = (metricSeries[k] || [])
          .filter((p) => p.value != null)
          .map((p) => ({ label: fmtClock(p.ts), value: Number(p.value) || 0 }));
        if (!pts.length) return null;
        return (
          <div className="section" key={k}>
            <div className="card chart-card">
              <div className="chart-title">{k.replace(/_/g, " ")} over time</div>
              <BarChart data={pts} formatValue={(v) => fmtNum(v)} />
            </div>
          </div>
        );
      })}
      <div className="section">
        <h3>Last cycle detail</h3>
        <div className="card cfg-card">
          {Object.entries(current?.detail || {}).map(([k, v]) => (
            <div className="cfg-row" key={k}>
              <div className="cfg-key">{k}</div>
              <div className="cfg-value"><span className="cfg-val">{v == null ? "—" : String(v)}</span></div>
            </div>
          ))}
          {!Object.keys(current?.detail || {}).length && <div className="empty">No detail in the latest sample.</div>}
        </div>
      </div>
    </>
  );
}

export function OverviewPanel() {
  const { data, error } = usePoll(() => api("/admin/overview"), LIVE_MS, [], "admin:overview");
  const [openDaemon, setOpenDaemon] = useState(null);
  const counts = data?.counts || {};
  const byFleet = counts.active_jobs_by_fleet || {};
  const activeJobs = Object.values(byFleet).reduce((a, b) => a + b, 0);
  const daemons = data?.daemons || [];
  const fleet = data?.fleet_availability;

  if (openDaemon) return <DaemonDetail name={openDaemon} onClose={() => setOpenDaemon(null)} />;

  return (
    <>
      <PanelHead title="System overview" sub="refreshes every 8s" />
      <ErrorBanner error={error} />
      <div className="tiles">
        <Tile label="Redis" value={<Pill tone={data?.redis_ok ? "good" : "critical"}>{data?.redis_ok ? "connected" : "down"}</Pill>} />
        <Tile label="Database" value={<Pill tone={data?.db_ok ? "good" : "critical"}>{data?.db_ok ? "connected" : "down"}</Pill>} />
        <Tile label="Active render groups" value={fmtNum(counts.active_groups)} />
        <Tile
          label="Active jobs"
          value={fmtNum(activeJobs)}
          hint={Object.entries(byFleet).map(([f, n]) => `${f}: ${n}`).join("  ") || undefined}
        />
        <Tile label="Dispatch queue" value={fmtNum(counts.dispatch_queue_depth)} />
        <Tile label="Pending allocation" value={fmtNum(counts.pending_allocation_depth)} />
        <Tile label="Machines alive" value={fmtNum(data?.machines_alive)} />
        <Tile label="Failures (24h)" value={fmtNum(counts.failures_24h)} />
      </div>

      <CostStrip />

      <div className="section">
        <h3>Background daemons <span className="sub">· singletons, one owner each · live health</span></h3>
        <div className="card">
          <DataTable
            emptyText="No daemon heartbeats yet (they publish within ~10s of a tick)."
            columns={[
              {
                key: "name", label: "Daemon",
                render: (d) => (
                  <span title={DAEMON_INFO[d.name]?.desc || ""}>
                    {DAEMON_INFO[d.name]?.label || d.name}
                  </span>
                ),
              },
              {
                key: "status", label: "Status",
                render: (d) => { const h = daemonHealth(d); return <Pill tone={h.tone}>{h.text}</Pill>; },
              },
              { key: "owner", label: "Owner instance", mono: true, render: (d) => shortId(d.owner) },
              { key: "tick_count", label: "Ticks", num: true, render: (d) => (d.reporting ? fmtNum(d.tick_count) : "–") },
              { key: "detail", label: "Last cycle", render: (d) => daemonDetail(d.detail) },
              {
                key: "error", label: "Last error",
                render: (d) => (d.error ? <span title={d.error} style={{ color: "var(--status-critical)" }}>{String(d.error).slice(0, 60)}</span> : "–"),
              },
            ]}
            rows={daemons}
            keyFn={(d) => d.name}
            onRowClick={(d) => d.reporting && setOpenDaemon(d.name)}
          />
        </div>
        <div className="hint" style={{ marginTop: 6 }}>
          Click a daemon for its history, activity graph & stats. Same owner across all rows = one instance holds every singleton (normal at 1 instance); heartbeats throttle to ~15s.
        </div>
      </div>

      <div className="section">
        <h3>Fleet availability (cached snapshot)</h3>
        <div className="card">
          {fleet ? (
            <DataTable
              columns={[
                { key: "fleet", label: "Fleet" },
                { key: "available", label: "Available", num: true },
                { key: "in_flight", label: "In flight", num: true },
              ]}
              rows={[
                { fleet: "vast", available: fleet.vast_available, in_flight: fleet.serverless_in_flight?.vast_serverless ?? 0 },
                { fleet: "modal", available: fleet.modal_available, in_flight: fleet.serverless_in_flight?.modal_serverless ?? 0 },
                { fleet: "community", available: fleet.community_available, in_flight: "–" },
              ]}
              keyFn={(r) => r.fleet}
            />
          ) : (
            <div className="empty">Snapshot not cached right now (rebuilds on the next allocation tick).</div>
          )}
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------- live renders

export function LiveRendersPanel() {
  const { data, error } = usePoll(() => api("/admin/render-groups/active"), LIVE_MS, [], "admin:live");
  const groups = data?.groups || [];
  return (
    <>
      <PanelHead title="Live renders" sub="all users · refreshes every 5s" />
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No active renders."
          columns={[
            {
              key: "user", label: "User",
              render: (g) => <span title={g.user_id}>{userLabel(g)}</span>,
            },
            { key: "input_filename", label: "File" },
            { key: "status", label: "Status", render: (g) => <Pill tone={statusTone(g.status)}>{g.status}</Pill> },
            { key: "tier", label: "Tier" },
            { key: "progress", label: "Progress", render: (g) => <Progress pct={g.overall_progress_pct} /> },
            {
              key: "frames", label: "Frames", num: true,
              render: (g) => `${fmtNum(g.overall_rendered_frames)} / ${fmtNum(g.total_frames)}`,
            },
            { key: "tasks_count", label: "Chunks", num: true },
            {
              key: "tokens", label: "Tokens (actual / est)", num: true,
              render: (g) => `${fmtCredits(g.total_actual_cost_credits)} / ${fmtCredits(g.total_estimated_cost_credits)}`,
            },
            { key: "submitted_at", label: "Submitted", render: (g) => fmtAgo(g.submitted_at) },
          ]}
          rows={groups}
          keyFn={(g) => g.group_id}
        />
      </div>
    </>
  );
}

// -------------------------------------------------------------------- jobs

function heartbeatPill(job) {
  if (job.heartbeat_alive === true) return <Pill tone="good">alive · {fmtSecs(job.heartbeat_age_sec)}</Pill>;
  if (job.heartbeat_alive === false) return <Pill tone="serious">stale{job.heartbeat_age_sec ? ` · ${fmtSecs(job.heartbeat_age_sec)}` : ""}</Pill>;
  return <Pill tone="neutral">unknown</Pill>;
}

export function JobsPanel() {
  const { data, error } = usePoll(() => api("/admin/jobs/active"), LIVE_MS, [], "admin:jobs");
  const jobs = data?.jobs || [];
  return (
    <>
      <PanelHead title="Active jobs" sub="pending + running chunks · refreshes every 5s" />
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No active jobs."
          columns={[
            { key: "id", label: "Job ID", mono: true },
            {
              key: "user", label: "User",
              render: (j) => <span title={j.user_id}>{userLabel(j)}</span>,
            },
            { key: "input_filename", label: "File" },
            { key: "status", label: "Status", render: (j) => <Pill tone={statusTone(j.status)}>{j.status}</Pill> },
            { key: "machine_type", label: "Fleet" },
            { key: "gpu_type", label: "GPU" },
            { key: "chunk_index", label: "Chunk", num: true },
            { key: "attempt", label: "Attempt", num: true },
            { key: "heartbeat_phase", label: "Phase" },
            { key: "hb", label: "Heartbeat", render: heartbeatPill },
            {
              key: "frames", label: "Frames", num: true,
              render: (j) => `${fmtNum(j.rendered_frames)} / ${fmtNum(j.total_frames)}`,
            },
            { key: "submitted_at", label: "Submitted", render: (j) => fmtAgo(j.submitted_at) },
          ]}
          rows={jobs}
          keyFn={(j) => j.id}
        />
      </div>
    </>
  );
}

// ---------------------------------------------------------------- machines

// Community-machine columns, shared by the Live (online-only) and History
// (full registry) sections of the Machines & instances tab.
const MACHINE_COLUMNS = [
  { key: "id", label: "Machine", mono: true, render: (m) => shortId(m.id) },
  { key: "gpu_model", label: "GPU" },
  { key: "gpu_vram_gb", label: "VRAM", num: true, render: (m) => `${m.gpu_vram_gb ?? "–"} GB` },
  { key: "cpu_cores", label: "CPU", num: true, render: (m) => m.cpu_cores ?? "–" },
  { key: "ram_gb", label: "RAM", num: true, render: (m) => (m.ram_gb ? `${m.ram_gb} GB` : "–") },
  {
    key: "alive", label: "Liveness",
    render: (m) => (m.alive
      ? <Pill tone="good">alive · {fmtSecs(m.heartbeat_age_sec)}</Pill>
      : <Pill tone="critical">offline</Pill>),
  },
  { key: "status", label: "DB status", render: (m) => <Pill tone={statusTone(m.status)}>{m.status}</Pill> },
  { key: "machine_type", label: "OS" },
  { key: "user_id", label: "Owner", mono: true, render: (m) => shortId(m.user_id) },
  { key: "registered_at", label: "Registered", render: (m) => fmtAgo(m.registered_at) },
  { key: "last_seen_at", label: "Last seen", render: (m) => fmtAgo(m.last_seen_at) },
];

// ------------------------------------------------------------------- users

export function UsersPanel() {
  const [selected, setSelected] = useState(null);
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/users"), SLOW_MS, [], "admin:users");
  const users = data?.users || [];

  if (selected) {
    return <UserDetail uid={selected} onBack={() => setSelected(null)} />;
  }
  return (
    <>
      <PanelHead title="Users" sub="click a row for detail">
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No users yet."
          onRowClick={(u) => setSelected(u.uid)}
          columns={[
            { key: "display_name", label: "Name", render: (u) => u.display_name || "(unnamed)" },
            { key: "email", label: "Email" },
            {
              key: "role", label: "Role",
              render: (u) => (u.role === "admin" ? <span className="badge admin">admin</span> : (u.role || "–")),
            },
            { key: "tier", label: "Tier" },
            { key: "credits", label: "Balance", num: true, render: (u) => fmtCredits(u.credits) },
            { key: "groups_count", label: "Renders", num: true },
            { key: "active_groups", label: "Active", num: true },
            { key: "actual_cost_credits", label: "Tokens spent", num: true, render: (u) => fmtCredits(u.actual_cost_credits) },
            { key: "actual_cost_usd", label: "Spend (USD)", num: true, render: (u) => fmtUsd(u.actual_cost_usd) },
            { key: "last_submitted_at", label: "Last render", render: (u) => fmtAgo(u.last_submitted_at) },
          ]}
          rows={users}
          keyFn={(u) => u.uid}
        />
      </div>
    </>
  );
}

function UserDetail({ uid, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api(`/admin/users/${uid}`).then(setData).catch(setError);
  }, [uid]);

  const profile = data?.profile || {};
  return (
    <>
      <PanelHead title={profile.display_name || uid} sub={profile.email}>
        <button className="back-link" onClick={onBack}>← all users</button>
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="section">
        <div className="card detail-grid">
          <div><div className="k">Role</div><div className="v">{profile.role || "user"}</div></div>
          <div><div className="k">Tier</div><div className="v">{profile.tier || "–"}</div></div>
          <div><div className="k">Credits</div><div className="v">{fmtCredits(profile.credits)}</div></div>
          <div><div className="k">Joined</div><div className="v">{fmtAgo(profile.created_at)}</div></div>
          <div><div className="k">UID</div><div className="v mono">{uid}</div></div>
        </div>
      </div>
      <div className="section">
        <h3>Recent renders</h3>
        <div className="card">
          <DataTable
            emptyText="No renders."
            columns={[
              { key: "id", label: "Group", mono: true, render: (g) => shortId(g.id) },
              { key: "input_filename", label: "File" },
              { key: "status", label: "Status", render: (g) => <Pill tone={statusTone(g.status)}>{g.status}</Pill> },
              { key: "tier", label: "Tier" },
              {
                key: "frames", label: "Frames", num: true,
                render: (g) => `${fmtNum(g.overall_rendered_frames)} / ${fmtNum(g.total_frames)}`,
              },
              { key: "total_actual_cost_credits", label: "Tokens", num: true, render: (g) => fmtCredits(g.total_actual_cost_credits) },
              { key: "total_actual_cost_usd", label: "Cost", num: true, render: (g) => fmtUsd(g.total_actual_cost_usd) },
              { key: "submitted_at", label: "Submitted", render: (g) => fmtAgo(g.submitted_at) },
            ]}
            rows={data?.recent_groups || []}
            keyFn={(g) => g.id}
          />
        </div>
      </div>
      <div className="section">
        <h3>Recent spend markers</h3>
        <div className="card">
          <DataTable
            emptyText="No spend recorded."
            columns={[
              { key: "task_id", label: "Task", mono: true, render: (s) => shortId(s.task_id) },
              { key: "total_credits_spent", label: "Credits", num: true, render: (s) => fmtCredits(s.total_credits_spent) },
              { key: "updated_at", label: "Updated", render: (s) => fmtAgo(s.updated_at) },
            ]}
            rows={data?.recent_spend || []}
            keyFn={(s) => s.task_id}
          />
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------------- costs

const COST_WINDOWS = [1, 7, 30, 90];

function ConfidencePill({ c }) {
  const tone = c === "exact" ? "good" : c === "estimated" ? "warning" : "neutral";
  return <Pill tone={tone}>{c}</Pill>;
}

function CostSourceDetail({ name, onClose }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let alive = true;
    api(`/admin/costs/source/${name}`).then((d) => alive && setData(d)).catch((e) => alive && setError(e));
    return () => { alive = false; };
  }, [name]);
  const monthly = data?.month_to_date_usd ?? data?.projected_monthly_usd ?? 0;
  const usage = data?.usage || {};
  return (
    <div className="section">
      <div className="panel-head">
        <button className="back-link" onClick={onClose}>← all sources</button>
        <h2 style={{ fontSize: 15, margin: 0 }}>{data?.label || name} <ConfidencePill c={data?.confidence || "estimated"} /></h2>
      </div>
      <ErrorBanner error={error} />
      {data?.note ? <div className="hint" style={{ marginBottom: 10 }}>{data.note}</div> : null}
      <div className="tiles">
        <Tile label={data?.month_to_date_usd != null ? "Month-to-date" : "Projected / month"} value={fmtUsd(monthly)} />
        <Tile label="Live burn" value={data?.live_rate_usd_per_hr != null ? `${fmtUsd(data.live_rate_usd_per_hr)}/hr` : "—"} />
      </div>
      {data?.series?.length ? (
        <div className="section">
          <div className="card chart-card">
            <div className="chart-title">Cost by day (USD)</div>
            <BarChart
              data={data.series.map((d) => ({ label: fmtDay(d.day), value: Number(d.usd) || 0 }))}
              formatValue={(v) => fmtUsd(v)}
            />
          </div>
        </div>
      ) : null}
      {data?.breakdown?.length ? (
        <div className="section">
          <h3>Breakdown</h3>
          <div className="card">
            <DataTable
              columns={[
                { key: "key", label: "Item" },
                { key: "usd", label: "USD", num: true, render: (r) => fmtUsd(r.usd) },
              ]}
              rows={data.breakdown}
              keyFn={(r) => r.key}
            />
          </div>
        </div>
      ) : null}
      {Object.keys(usage).length ? (
        <div className="section">
          <h3>Usage</h3>
          <div className="card cfg-card">
            {Object.entries(usage).map(([k, v]) => (
              <div className="cfg-row" key={k}>
                <div className="cfg-key">{k}</div>
                <div className="cfg-value"><span className="cfg-val">{v == null ? "—" : String(v)}</span></div>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function AllSourcesCosts() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/costs/overview"), SLOW_MS, [], "admin:costs:overview");
  const [open, setOpen] = useState(null);
  const sources = data?.sources || [];
  if (open) return <CostSourceDetail name={open} onClose={() => setOpen(null)} />;
  return (
    <>
      <PanelHead title="Cost — all sources" sub="month-to-date + live burn · every paid dependency">
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="tiles">
        <Tile label="This month · all sources" value={fmtUsd(data?.total_month_to_date_usd)} hint="exact where known, else estimated" />
        <Tile label="Live burn" value={`${fmtUsd(data?.total_live_rate_usd_per_hr)}/hr`} hint="current $/hour across sources" />
        <Tile label="Sources tracked" value={fmtNum(sources.length)} />
      </div>
      <div className="section">
        <h3>By source <span className="sub">· click a card for detail, graphs & usage</span></h3>
        <div className="cost-cards">
          {sources.map((s) => (
            <button key={s.name} className="cost-card" onClick={() => setOpen(s.name)}>
              <div className="cc-head">
                <span className="cc-label">{s.label}</span>
                <ConfidencePill c={s.confidence} />
              </div>
              <div className="cc-amount">
                {fmtUsd(s.month_to_date_usd ?? s.projected_monthly_usd ?? 0)}
                <span className="cc-unit">{s.month_to_date_usd != null ? " this mo" : " /mo est"}</span>
              </div>
              <div className="cc-sub">{s.live_rate_usd_per_hr ? `${fmtUsd(s.live_rate_usd_per_hr)}/hr live` : " "}</div>
              <div className="cc-note">{s.note}</div>
            </button>
          ))}
          {sources.length === 0 && <div className="empty">No cost sources reporting yet.</div>}
        </div>
      </div>
    </>
  );
}

export function CostsPanel() {
  const [view, setView] = useState("sources");
  return (
    <>
      <div className="panel-head">
        <div className="subtabs">
          <button className={`subtab${view === "sources" ? " active" : ""}`} onClick={() => setView("sources")}>All sources</button>
          <button className={`subtab${view === "render" ? " active" : ""}`} onClick={() => setView("render")}>Render detail</button>
        </div>
      </div>
      {view === "sources" ? <AllSourcesCosts /> : <RenderCostDetail />}
    </>
  );
}

function RenderCostDetail() {
  const [days, setDays] = useState(7);
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api(`/admin/costs/summary?days=${days}`), SLOW_MS, [days], `admin:costs:${days}`);
  const totals = data?.totals || {};
  return (
    <>
      <PanelHead title="Render cost detail" sub={`render_telemetry · last ${days} day(s)`}>
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
        {COST_WINDOWS.map((d) => (
          <button
            key={d}
            className={`btn ghost small${d === days ? " active" : ""}`}
            style={d === days ? { borderColor: "var(--series-1)", color: "var(--ink)" } : undefined}
            onClick={() => setDays(d)}
          >
            {d}d
          </button>
        ))}
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="tiles">
        <Tile label="Tokens spent" value={fmtCredits(totals.actual_credits)} hint={`${fmtUsd(totals.actual_usd)} actual cost`} />
        <Tile label="Tokens estimated" value={fmtCredits(totals.estimated_credits)} hint={`${fmtUsd(totals.estimated_usd)} estimated`} />
        <Tile label="Chunks rendered" value={fmtNum(totals.chunks)} />
        <Tile label="Frames" value={fmtNum(totals.frames)} />
        <Tile label="GPU time" value={fmtHours(totals.seconds_total)} />
      </div>

      <div className="section">
        <div className="card chart-card">
          <div className="chart-title">Tokens spent by day</div>
          <BarChart
            data={(data?.by_day || []).map((d) => ({ label: fmtDay(d.day), value: Number(d.actual_credits) || 0 }))}
            formatValue={(v) => `${fmtCredits(v)} tokens`}
          />
        </div>
      </div>

      <div className="section">
        <h3>By fleet</h3>
        <div className="card">
          <DataTable
            columns={[
              { key: "fleet", label: "Fleet" },
              { key: "chunks", label: "Chunks", num: true },
              { key: "seconds_total", label: "GPU time", num: true, render: (r) => fmtHours(r.seconds_total) },
              { key: "actual_credits", label: "Tokens", num: true, render: (r) => fmtCredits(r.actual_credits) },
              { key: "actual_usd", label: "Cost (USD)", num: true, render: (r) => fmtUsd(r.actual_usd) },
            ]}
            rows={data?.by_fleet || []}
            keyFn={(r) => r.fleet}
          />
        </div>
      </div>

      <div className="section">
        <h3>By GPU</h3>
        <div className="card">
          <DataTable
            columns={[
              { key: "gpu", label: "GPU" },
              { key: "chunks", label: "Chunks", num: true },
              { key: "seconds_total", label: "GPU time", num: true, render: (r) => fmtHours(r.seconds_total) },
              { key: "actual_credits", label: "Tokens", num: true, render: (r) => fmtCredits(r.actual_credits) },
              { key: "actual_usd", label: "Cost (USD)", num: true, render: (r) => fmtUsd(r.actual_usd) },
            ]}
            rows={data?.by_gpu || []}
            keyFn={(r) => r.gpu}
          />
        </div>
      </div>

      <div className="section">
        <h3>Top users</h3>
        <div className="card">
          <DataTable
            columns={[
              { key: "user", label: "User", render: (r) => <span title={r.user_id}>{userLabel(r)}</span> },
              { key: "chunks", label: "Chunks", num: true },
              { key: "actual_credits", label: "Tokens", num: true, render: (r) => fmtCredits(r.actual_credits) },
              { key: "actual_usd", label: "Cost (USD)", num: true, render: (r) => fmtUsd(r.actual_usd) },
            ]}
            rows={data?.top_users || []}
            keyFn={(r) => r.user_id}
          />
        </div>
      </div>
    </>
  );
}

// ------------------------------------------------------------- cloud (gcp)

export function GcpPanel() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/gcp/metrics?days=30"), SLOW_MS, [], "admin:gcp");
  const daySeries = (arr, unit) => (arr || []).map((d) => ({ label: fmtDay(d.day), value: Number(d.value) || 0 }));
  return (
    <>
      <PanelHead title="Cloud (GCP)" sub="Cloud Run usage & cost · every service in the project (dev/staging/prod) · last 30d">
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
      </PanelHead>
      <ErrorBanner error={error} />
      {data && data.available === false ? (
        <div className="notice">
          GCP metrics aren&apos;t available yet — {data.note || data.error}
          <div style={{ marginTop: 8 }}>
            <code>gcloud projects add-iam-policy-binding gen-lang-client-0545494042 --member=&quot;serviceAccount:&lt;runtime-SA&gt;&quot; --role=&quot;roles/monitoring.viewer&quot;</code>
          </div>
          <div style={{ marginTop: 6 }}>No key needed — the app reads Monitoring via the runtime service account. Then hit Refresh.</div>
        </div>
      ) : !data ? (
        <div className="loading">Loading GCP metrics…</div>
      ) : (
        <>
          <div className="tiles">
            <Tile label="Cloud Run cost · 30d (est)" value={fmtUsd(data.total_est_cost_usd)} hint="billable time × price" />
            <Tile label="Requests · 30d" value={fmtNum(data.total_requests)} />
            <Tile label="Services" value={fmtNum(data.services?.length)} hint={`project ${data.project || "?"}`} />
          </div>
          {(data.services || []).map((s) => (
            <div className="section" key={s.service}>
              <h3>{s.service} <span className="sub">· {fmtNum(s.requests_total)} req · {fmtUsd(s.est_cost_usd)} est · 30d</span></h3>
              <div className="card chart-card">
                <div className="chart-title">Requests / day</div>
                <BarChart data={daySeries(s.requests_series)} formatValue={(v) => `${fmtNum(v)} req`} />
              </div>
              <div className="card chart-card" style={{ marginTop: 10 }}>
                <div className="chart-title">Billable instance-seconds / day</div>
                <BarChart data={daySeries(s.billable_series)} formatValue={(v) => fmtNum(v)} />
              </div>
              <div className="card chart-card" style={{ marginTop: 10 }}>
                <div className="chart-title">Avg instances / day</div>
                <BarChart data={daySeries(s.instances_series)} formatValue={(v) => fmtNum(v)} />
              </div>
            </div>
          ))}
          {(!data.services || data.services.length === 0) && (
            <div className="card"><div className="empty">No Cloud Run series returned for this window.</div></div>
          )}
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------- failures

export function FailuresPanel() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/failures/recent?limit=100"), SLOW_MS, [], "admin:failures");
  return (
    <>
      <PanelHead title="Failure events" sub="most recent first">
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No failures recorded. Nice."
          columns={[
            { key: "completed_at", label: "When", render: (f) => fmtAgo(f.completed_at) },
            { key: "fleet", label: "Fleet" },
            { key: "user", label: "User", render: (f) => <span title={f.user_id}>{userLabel(f)}</span> },
            { key: "input_filename", label: "File" },
            { key: "job_id", label: "Job", mono: true, render: (f) => shortId(f.job_id) },
            { key: "group_id", label: "Group", mono: true, render: (f) => shortId(f.group_id) },
            { key: "retry_count", label: "Retries", num: true, render: (f) => fmtNum(f.retry_count) },
            {
              key: "failure_reason", label: "Reason",
              render: (f) => <span title={f.failure_reason}><Pill tone="serious">{(f.failure_reason || "").slice(0, 90) || "—"}</Pill></span>,
            },
          ]}
          rows={data?.failures || []}
          keyFn={(f) => f.id}
        />
      </div>
    </>
  );
}

// -------------------------------------------------------------------- logs

export function LogsPanel() {
  const [lines, setLines] = useState([]);
  const [live, setLive] = useState(true);
  const [error, setError] = useState(null);
  const boxRef = useRef(null);
  const sourceRef = useRef(null);

  useEffect(() => {
    api("/logs/recent?limit=300")
      .then((initial) => setLines(initial))
      .catch(setError);
  }, []);

  useEffect(() => {
    if (!live) {
      sourceRef.current?.close();
      sourceRef.current = null;
      return undefined;
    }
    let cancelled = false;
    (async () => {
      const url = await sseUrl("/logs/stream");
      if (cancelled) return;
      const es = new EventSource(url);
      es.onmessage = (event) => {
        try {
          const entry = JSON.parse(event.data);
          setLines((prev) => [...prev.slice(-499), entry]);
        } catch {
          /* skip malformed line */
        }
      };
      es.onerror = () => es.close();
      sourceRef.current = es;
    })();
    return () => {
      cancelled = true;
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [live]);

  useEffect(() => {
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  return (
    <>
      <PanelHead
        title="Server logs"
        sub="in-memory ring of the instance serving this request"
      >
        <button className="btn ghost small" onClick={() => setLive((v) => !v)}>
          {live ? "pause live tail" : "resume live tail"}
        </button>
      </PanelHead>
      <ErrorBanner error={error} />
      <div className="hint" style={{ marginBottom: 8 }}>
        Per-instance view — this is only the Cloud Run instance that answered your request.
        For logs across every instance, use Cloud Logging (Cloud Run ships all stdout there automatically).
      </div>
      <div className="card logbox" ref={boxRef}>
        {lines.length === 0 ? (
          <div className="empty">No log lines yet.</div>
        ) : (
          lines.map((l, i) => (
            <div className="logline" key={i}>
              <span className={`lvl ${l.level}`}>{l.level}</span>
              <span className="lg">{l.logger}</span>
              <span className="msg">{l.message}</span>
            </div>
          ))
        )}
      </div>
    </>
  );
}

// --------------------------------------------------------------- downloads

export function DownloadsPanel() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/downloads"), SLOW_MS, [], "admin:downloads");
  const downloads = data?.downloads || {};
  const kinds = Object.entries(downloads);
  return (
    <>
      <PanelHead title="Downloads" sub="agent installers + output zips · counted since this feature shipped">
        <RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />
      </PanelHead>
      <ErrorBanner error={error} />
      {kinds.length === 0 ? (
        <div className="card"><div className="empty">No downloads counted yet.</div></div>
      ) : (
        <>
          <div className="tiles">
            {kinds.map(([kind, stats]) => (
              <Tile key={kind} label={kind.replace(/_/g, " ")} value={fmtNum(stats.total)} />
            ))}
          </div>
          <div className="section">
            <h3>By day</h3>
            <div className="card">
              <DataTable
                columns={[
                  { key: "day", label: "Day" },
                  ...kinds.map(([kind]) => ({
                    key: kind, label: kind.replace(/_/g, " "), num: true,
                    render: (r) => fmtNum(r[kind] || 0),
                  })),
                ]}
                rows={mergeByDay(downloads)}
                keyFn={(r) => r.day}
              />
            </div>
          </div>
        </>
      )}
    </>
  );
}

function mergeByDay(downloads) {
  const days = {};
  for (const [kind, stats] of Object.entries(downloads)) {
    for (const [day, count] of Object.entries(stats.by_day || {})) {
      days[day] = days[day] || { day: fmtDay(day), _sort: day };
      days[day][kind] = count;
    }
  }
  return Object.values(days).sort((a, b) => b._sort.localeCompare(a._sort)).slice(0, 30);
}

// ------------------------------------------------------------- redis activity

// Upstash free tier is 500k commands/month; used only to color the projection.
const REDIS_MONTHLY_QUOTA = 500000;

export function RedisPanel() {
  // Command stream: in-memory proxy, ZERO Redis ops — safe at a live cadence.
  const { data, error } = usePoll(() => api("/admin/redis?limit=250"), LIVE_MS, [], "admin:redis");
  // Fleet INFO: real Redis reads — same 60s floor so watching this panel
  // doesn't burn the very quota it displays.
  const { data: srv } = usePoll(() => api("/admin/redis/server"), LIVE_MS, [], "admin:redis:server");
  const [resetting, setResetting] = useState(false);

  const server = srv?.server || null;
  const instanceOn = data ? data.enabled !== false : true;
  const servedBy = srv?.served_by_instance || data?.served_by_instance;

  // Fleet-wide (Redis INFO): the server's own global counters — every instance
  // plus the backup-monitor cron. This is the real quota number.
  const fleetOps = server?.instantaneous_ops_per_sec ?? 0;
  const fleetProjected = fleetOps * 60 * 60 * 24 * 30;
  const fleetPct = Math.min(100, (fleetProjected / REDIS_MONTHLY_QUOTA) * 100);
  const fleetTone = fleetPct > 90 ? "critical" : fleetPct > 60 ? "warning" : "good";
  const hits = server?.keyspace_hits ?? 0;
  const misses = server?.keyspace_misses ?? 0;
  const hitRate = hits + misses > 0 ? (hits / (hits + misses)) * 100 : null;

  // This instance (in-memory proxy): per-command detail, zero Redis ops.
  const projected = data?.projected_per_month ?? 0;
  const quotaPct = Math.min(100, (projected / REDIS_MONTHLY_QUOTA) * 100);
  const quotaTone = quotaPct > 90 ? "critical" : quotaPct > 60 ? "warning" : "good";
  const byPurpose = Object.entries(data?.by_purpose || {}).map(([purpose, count]) => ({ purpose, count }));
  const byCommand = Object.entries(data?.by_command || {}).map(([command, count]) => ({ command, count }));
  const recent = data?.recent || [];

  const doReset = async () => {
    setResetting(true);
    try { await api("/admin/redis/reset", { method: "POST" }); } finally { setResetting(false); }
  };

  return (
    <>
      <PanelHead title="Redis activity" sub="fleet-wide quota (Redis INFO) + this instance's live command detail" />
      <ErrorBanner error={error} />

      {/* -------- fleet-wide, from Redis' own INFO -------- */}
      <div className="section">
        <h3>Whole Redis <span className="sub">· all instances + backup-monitor cron · from Redis INFO (read-only)</span></h3>
        {server ? (
          <div className="tiles">
            <Tile label="Ops / sec (now)" value={fmtNum(fleetOps)} hint="Redis instantaneous rate" />
            <Tile
              label="Projected / month"
              value={<Pill tone={fleetTone}>{fmtNum(fleetProjected)}</Pill>}
              hint={`${fleetPct.toFixed(0)}% of 500k · at current ops/sec`}
            />
            <Tile label="Commands processed" value={fmtNum(server.total_commands_processed)} hint="server total since uptime" />
            <Tile label="Keys in DB" value={fmtNum(server.dbsize)} />
            <Tile label="Connected clients" value={fmtNum(server.connected_clients)} />
            <Tile label="Memory used" value={server.used_memory_human || "—"} />
            <Tile
              label="Keyspace hit rate"
              value={hitRate == null ? "—" : `${hitRate.toFixed(1)}%`}
              hint={`${fmtNum(hits)} hits / ${fmtNum(misses)} misses`}
            />
            <Tile label="Redis uptime" value={fmtSecs(server.uptime_in_seconds)} />
          </div>
        ) : (
          <div className="card"><div className="empty">Redis INFO unavailable on this server.</div></div>
        )}
      </div>

      {/* -------- this instance, from the in-memory proxy -------- */}
      <div className="panel-head">
        <h2 style={{ fontSize: 14 }}>
          This instance
          <span className="sub">
            {" · "}{servedBy ? `${servedBy.slice(0, 12)} · ` : ""}per-command detail, in-memory · zero Redis ops · 60s
          </span>
        </h2>
        <div className="spacer" />
        {instanceOn && (
          <button className="btn ghost small" onClick={doReset} disabled={resetting}>
            {resetting ? "resetting…" : "reset counters"}
          </button>
        )}
      </div>

      {!instanceOn ? (
        <div className="notice">
          Per-command logging is off on the instance that answered. Set
          {" "}<code>REDIS_CMD_LOG=1</code> (the default) to capture the stream.
          It records in memory only — zero Redis ops.
        </div>
      ) : (
        <>
          <div className="tiles">
            <Tile label="Requests / min (now)" value={fmtNum(data?.rate_per_min)} hint="rolling 60s · this instance" />
            <Tile
              label="Projected / month"
              value={<Pill tone={quotaTone}>{fmtNum(projected)}</Pill>}
              hint={`${quotaPct.toFixed(0)}% of 500k · this instance only`}
            />
            <Tile label="Projected / day" value={fmtNum(data?.projected_per_day)} />
            <Tile label="Total since reset" value={fmtNum(data?.total)} hint={`avg ${fmtNum(data?.avg_per_min_since_start)}/min`} />
          </div>

          <div className="section">
            <h3>By purpose <span className="sub">· what the commands are for</span></h3>
            <div className="card">
              <DataTable
                columns={[
                  { key: "purpose", label: "Purpose" },
                  { key: "count", label: "Commands", num: true, render: (r) => fmtNum(r.count) },
                  {
                    key: "share", label: "Share", num: true,
                    render: (r) => `${((r.count / Math.max(1, data?.total || 1)) * 100).toFixed(0)}%`,
                  },
                ]}
                rows={byPurpose}
                keyFn={(r) => r.purpose}
              />
            </div>
          </div>

          <div className="section">
            <h3>By command</h3>
            <div className="card">
              <DataTable
                columns={[
                  { key: "command", label: "Command", mono: true },
                  { key: "count", label: "Count", num: true, render: (r) => fmtNum(r.count) },
                ]}
                rows={byCommand}
                keyFn={(r) => r.command}
              />
            </div>
          </div>

          <div className="section">
            <h3>Live command stream <span className="sub">· newest first, last {recent.length}</span></h3>
            <div className="card">
              <DataTable
                emptyText="No commands captured yet."
                columns={[
                  { key: "seq", label: "#", num: true },
                  { key: "ts", label: "Time", mono: true, render: (r) => fmtClock(r.ts) },
                  { key: "command", label: "Command", mono: true, render: (r) => r.command.toUpperCase() },
                  { key: "rw", label: "R/W", render: (r) => (r.write ? <Pill tone="warning">write</Pill> : <Pill tone="neutral">read</Pill>) },
                  { key: "key", label: "Key", mono: true },
                  { key: "purpose", label: "Purpose" },
                  { key: "source", label: "Via" },
                ]}
                rows={recent}
                keyFn={(r) => r.seq}
              />
            </div>
          </div>
        </>
      )}
    </>
  );
}

// =========================================================================
// Machines & instances — one tab, two subtabs (Live / History).
//   Live    : serverless containers running now + community machines online.
//   History : the full machine registry (incl. offline) + completed-run
//             records (the only history serverless instances have).
// =========================================================================

const INSTANCE_COLUMNS = [
  { key: "fleet_type", label: "Fleet", render: (i) => (i.fleet_type || "").replace("_serverless", "") },
  { key: "job_id", label: "Job ID", mono: true },
  { key: "user", label: "User", render: (i) => <span title={i.user_id}>{userLabel(i)}</span> },
  { key: "input_filename", label: "File" },
  { key: "gpu_label", label: "GPU" },
  { key: "provider_status", label: "Provider status", render: (i) => <Pill tone={statusTone(i.provider_status)}>{i.provider_status || "—"}</Pill> },
  { key: "frames", label: "Frames", num: true, render: (i) => `${fmtNum(i.rendered_frames)} / ${fmtNum(i.total_frames)}` },
  { key: "elapsed_sec", label: "Elapsed", num: true, render: (i) => fmtSecs(i.elapsed_sec) },
  { key: "live_cost_credits", label: "Cost so far (tokens)", num: true, render: (i) => fmtCredits(i.live_cost_credits) },
  { key: "live_cost_usd", label: "Cost so far (USD)", num: true, render: (i) => fmtUsd(i.live_cost_usd) },
  { key: "error", label: "Error", render: (i) => (i.error ? <Pill tone="critical">error</Pill> : "—") },
];

const RUN_HISTORY_COLUMNS = [
  { key: "completed_at", label: "Completed", render: (r) => fmtAgo(r.completed_at) },
  { key: "user", label: "User", render: (r) => <span title={r.user_id}>{userLabel(r)}</span> },
  { key: "input_filename", label: "File" },
  { key: "fleet", label: "Fleet" },
  { key: "machine_id", label: "Machine/Instance", mono: true, render: (r) => (r.machine_id ? shortId(r.machine_id) : shortId(r.job_id)) },
  { key: "gpu", label: "GPU", render: (r) => r.gpu_model_normalized || r.gpu_type || "—" },
  { key: "device_used", label: "Device", render: (r) => r.device_used || "—" },
  { key: "peak_vram_mb", label: "Peak VRAM", num: true, render: (r) => (r.peak_vram_mb ? `${(r.peak_vram_mb / 1024).toFixed(1)} GB` : "—") },
  { key: "cpu_cores", label: "CPU", num: true, render: (r) => r.cpu_cores ?? "—" },
  { key: "ram_gb", label: "RAM", num: true, render: (r) => (r.ram_gb ? `${r.ram_gb} GB` : "—") },
  { key: "rendered_frames", label: "Frames", num: true, render: (r) => fmtNum(r.rendered_frames) },
  { key: "seconds_total", label: "Runtime", num: true, render: (r) => fmtSecs(r.seconds_total) },
  { key: "startup_seconds", label: "Startup", num: true, render: (r) => (r.startup_seconds != null ? fmtSecs(r.startup_seconds) : "—") },
  { key: "blender_version", label: "Blender", render: (r) => r.blender_version || "—" },
  { key: "cost_actual_credits", label: "Tokens", num: true, render: (r) => fmtCredits(r.cost_actual_credits) },
  { key: "cost_actual_usd", label: "Cost", num: true, render: (r) => fmtUsd(r.cost_actual_usd) },
];

function Section({ title, sub, error, action, children }) {
  return (
    <div className="section">
      <h3 style={action ? { display: "flex", alignItems: "baseline" } : undefined}>
        <span>{title}{sub ? <span className="sub"> · {sub}</span> : null}</span>
        {action ? <span className="spacer" /> : null}
        {action}
      </h3>
      <ErrorBanner error={error} />
      <div className="card">{children}</div>
    </div>
  );
}

function LiveInstancesSection() {
  const { data, error } = usePoll(() => api("/admin/instances"), LIVE_MS, [], "admin:instances");
  const servedBy = data?.served_by;
  // Base rows come from Postgres (fleet-correct); "live" rows are additionally
  // enriched with provider status/logs by the instance running that monitor.
  const cols = [
    ...INSTANCE_COLUMNS,
    {
      key: "source", label: "Detail",
      render: (i) => (i.source === "live"
        ? <Pill tone="good">live</Pill>
        : <Pill tone="neutral">db</Pill>),
    },
  ];
  return (
    <Section
      title="Serverless instances (live)"
      sub={`Vast / Modal running now · base from Postgres${servedBy ? ` · live detail via ${servedBy.slice(0, 8)}` : ""}`}
      error={error}
    >
      <DataTable
        emptyText="No serverless containers running right now."
        columns={cols}
        rows={data?.instances || []}
        keyFn={(i) => i.job_id}
      />
    </Section>
  );
}

function LiveMachinesSection() {
  const { data, error } = usePoll(() => api("/admin/machines"), LIVE_MS, [], "admin:machines");
  const online = (data?.machines || []).filter((m) => m.alive);
  return (
    <Section title="Community machines (online)" sub="alive heartbeat now" error={error}>
      <DataTable
        emptyText="No community machines online right now."
        columns={MACHINE_COLUMNS}
        rows={online}
        keyFn={(m) => m.id}
      />
    </Section>
  );
}

function MachineRegistrySection() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/machines"), SLOW_MS, [], "admin:machines");
  return (
    <Section title="All registered machines" sub="full registry, incl. offline" error={error}
      action={<RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />}>
      <DataTable
        emptyText="No machines have ever registered."
        columns={MACHINE_COLUMNS}
        rows={data?.machines || []}
        keyFn={(m) => m.id}
      />
    </Section>
  );
}

function RunHistorySection() {
  const { data, error, updatedAt, refresh, loading } = usePoll(() => api("/admin/telemetry?limit=200"), SLOW_MS, [], "admin:telemetry");
  return (
    <Section title="Completed runs" sub="per finished chunk, newest first" error={error}
      action={<RefreshBar updatedAt={updatedAt} loading={loading} onRefresh={refresh} intervalMs={SLOW_MS} />}>
      <DataTable
        emptyText="No completed-chunk telemetry yet."
        columns={RUN_HISTORY_COLUMNS}
        rows={data?.telemetry || []}
        keyFn={(r) => r.id}
      />
    </Section>
  );
}

export function ComputePanel() {
  const [sub, setSub] = useState("live");
  return (
    <>
      <PanelHead title="Machines & instances">
        <div className="subtabs">
          <button className={`subtab${sub === "live" ? " active" : ""}`} onClick={() => setSub("live")}>Live</button>
          <button className={`subtab${sub === "history" ? " active" : ""}`} onClick={() => setSub("history")}>History</button>
        </div>
      </PanelHead>
      {sub === "live" ? (
        <>
          <LiveInstancesSection />
          <LiveMachinesSection />
        </>
      ) : (
        <>
          <MachineRegistrySection />
          <RunHistorySection />
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------- config

function ConfigTree({ value }) {
  if (value === null || value === undefined) return <span className="cfg-val muted">—</span>;
  if (typeof value !== "object") {
    return <span className="cfg-val">{String(value)}</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="cfg-val muted">[]</span>;
    // Array of primitives -> inline; array of objects -> a small table-ish list.
    if (typeof value[0] !== "object") {
      return <span className="cfg-val">{value.join(", ")}</span>;
    }
    return (
      <div className="cfg-array">
        {value.map((item, i) => (
          <div className="cfg-array-item" key={i}>
            <ConfigTree value={item} />
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="cfg-obj">
      {Object.entries(value).map(([k, v]) => (
        <div className="cfg-row" key={k}>
          <div className="cfg-key">{k}</div>
          <div className="cfg-value"><ConfigTree value={v} /></div>
        </div>
      ))}
    </div>
  );
}

export function ConfigPanel() {
  const [global, setGlobal] = useState(null);
  const [cost, setCost] = useState(null);
  const [pricing, setPricing] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api("/admin/config").then((d) => setGlobal(d.config)).catch(setError);
    api("/admin/config/cost-estimation").then((d) => setCost(d.config)).catch(() => {});
    api("/admin/config/cost-pricing").then((d) => setPricing(d.config)).catch(() => {});
  }, []);

  const sections = global ? Object.entries(global) : [];
  return (
    <>
      <PanelHead title="Configuration" sub="read-only view of Firestore config/global + cost_estimation + cost_pricing" />
      <ErrorBanner error={error} />
      {pricing ? (
        <div className="section">
          <h3>Cost pricing <span className="sub">· config/cost_pricing · unit prices for cost estimates · edit in Firestore</span></h3>
          <div className="card cfg-card"><ConfigTree value={pricing} /></div>
        </div>
      ) : null}
      {cost ? (
        <div className="section">
          <h3>Cost estimation (config/cost_estimation)</h3>
          <div className="card cfg-card"><ConfigTree value={cost} /></div>
        </div>
      ) : null}
      {!global ? (
        <div className="loading">Loading config…</div>
      ) : (
        sections.map(([name, val]) => (
          <div className="section" key={name}>
            <h3>{name}</h3>
            <div className="card cfg-card"><ConfigTree value={val} /></div>
          </div>
        ))
      )}
    </>
  );
}
