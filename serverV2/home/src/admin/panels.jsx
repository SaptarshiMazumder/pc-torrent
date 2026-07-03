// Admin panels — each backs one tab and polls its /admin/* endpoint.

import { useEffect, useRef, useState } from "react";
import { api, sseUrl } from "../api.js";
import {
  fmtAgo, fmtClock, fmtCredits, fmtDay, fmtHours, fmtNum, fmtSecs, fmtUsd, shortId,
} from "../format.js";
import {
  BarChart, DataTable, Pill, Progress, Tile, statusTone, usePoll,
} from "../components/ui.jsx";

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

export function OverviewPanel() {
  const { data, error } = usePoll(() => api("/admin/overview"), 8000, [], "admin:overview");
  const counts = data?.counts || {};
  const byFleet = counts.active_jobs_by_fleet || {};
  const activeJobs = Object.values(byFleet).reduce((a, b) => a + b, 0);
  const daemons = data?.daemons || [];
  const fleet = data?.fleet_availability;

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
          />
        </div>
        <div className="hint" style={{ marginTop: 6 }}>
          Same owner across all rows = one instance holds every singleton (normal at 1 instance). Heartbeats are throttled to ~15s and cost one shared Redis hash.
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
  const { data, error } = usePoll(() => api("/admin/render-groups/active"), 5000, [], "admin:live");
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
  const { data, error } = usePoll(() => api("/admin/jobs/active"), 5000, [], "admin:jobs");
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
  const { data, error } = usePoll(() => api("/admin/users"), 60000, [], "admin:users");
  const users = data?.users || [];

  if (selected) {
    return <UserDetail uid={selected} onBack={() => setSelected(null)} />;
  }
  return (
    <>
      <PanelHead title="Users" sub="cached 60s · click a row for detail" />
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

export function CostsPanel() {
  const [days, setDays] = useState(7);
  const { data, error } = usePoll(() => api(`/admin/costs/summary?days=${days}`), 60000, [days], `admin:costs:${days}`);
  const totals = data?.totals || {};
  return (
    <>
      <PanelHead title="Costs" sub={`render_telemetry · last ${days} day(s) · cached 60s`}>
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

// ---------------------------------------------------------------- failures

export function FailuresPanel() {
  const { data, error } = usePoll(() => api("/admin/failures/recent?limit=100"), 30000, [], "admin:failures");
  return (
    <>
      <PanelHead title="Failure events" sub="most recent first · refreshes every 30s" />
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
  const { data, error } = usePoll(() => api("/admin/downloads"), 60000, [], "admin:downloads");
  const downloads = data?.downloads || {};
  const kinds = Object.entries(downloads);
  return (
    <>
      <PanelHead title="Downloads" sub="agent installers + output zips · counted since this feature shipped" />
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
  const { data, error } = usePoll(() => api("/admin/redis?limit=250"), 2000, [], "admin:redis");
  const [resetting, setResetting] = useState(false);

  if (data && data.enabled === false) {
    return (
      <>
        <PanelHead title="Redis activity" />
        <div className="notice">
          Redis command logging is disabled on this server. Set
          {" "}<code>REDIS_CMD_LOG=1</code> (the default) and redeploy to capture
          every command. It records in memory only — it adds zero Redis ops.
        </div>
      </>
    );
  }

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
      <PanelHead title="Redis activity" sub="every command this server sends · live, 2s refresh">
        <button className="btn ghost small" onClick={doReset} disabled={resetting}>
          {resetting ? "resetting…" : "reset counters"}
        </button>
      </PanelHead>
      <ErrorBanner error={error} />

      <div className="tiles">
        <Tile label="Requests / min (now)" value={fmtNum(data?.rate_per_min)} hint="rolling last 60s" />
        <Tile
          label="Projected / month"
          value={<Pill tone={quotaTone}>{fmtNum(projected)}</Pill>}
          hint={`${quotaPct.toFixed(0)}% of 500k free tier`}
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

function Section({ title, sub, error, children }) {
  return (
    <div className="section">
      <h3>{title}{sub ? <span className="sub"> · {sub}</span> : null}</h3>
      <ErrorBanner error={error} />
      <div className="card">{children}</div>
    </div>
  );
}

function LiveInstancesSection() {
  const { data, error } = usePoll(() => api("/admin/instances"), 5000, [], "admin:instances");
  return (
    <Section title="Serverless instances (live)" sub="Vast / Modal running now" error={error}>
      <DataTable
        emptyText="No serverless containers running right now."
        columns={INSTANCE_COLUMNS}
        rows={data?.instances || []}
        keyFn={(i) => i.job_id}
      />
    </Section>
  );
}

function LiveMachinesSection() {
  const { data, error } = usePoll(() => api("/admin/machines"), 15000, [], "admin:machines");
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
  const { data, error } = usePoll(() => api("/admin/machines"), 30000, [], "admin:machines");
  return (
    <Section title="All registered machines" sub="full registry, incl. offline" error={error}>
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
  const { data, error } = usePoll(() => api("/admin/telemetry?limit=200"), 30000, [], "admin:telemetry");
  return (
    <Section title="Completed runs" sub="per finished chunk, newest first" error={error}>
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
  const [error, setError] = useState(null);

  useEffect(() => {
    api("/admin/config").then((d) => setGlobal(d.config)).catch(setError);
    api("/admin/config/cost-estimation").then((d) => setCost(d.config)).catch(() => {});
  }, []);

  const sections = global ? Object.entries(global) : [];
  return (
    <>
      <PanelHead title="Configuration" sub="read-only view of Firestore config/global + config/cost_estimation" />
      <ErrorBanner error={error} />
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
