// Admin panels — each backs one tab and polls its /admin/* endpoint.

import { useEffect, useRef, useState } from "react";
import { api, sseUrl } from "../api.js";
import {
  fmtAgo, fmtCredits, fmtDay, fmtHours, fmtNum, fmtSecs, fmtUsd, shortId,
} from "../format.js";
import {
  BarChart, DataTable, Pill, Progress, Tile, statusTone, usePoll,
} from "../components/ui.jsx";

// Human identity for a row that carries user_email / user_display_name
// (annotated server-side), falling back to the short uid.
function userLabel(row) {
  return row.user_email || row.user_display_name || shortId(row.user_id);
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
  const { data, error } = usePoll(() => api("/admin/overview"), 5000);
  const counts = data?.counts || {};
  const byFleet = counts.active_jobs_by_fleet || {};
  const activeJobs = Object.values(byFleet).reduce((a, b) => a + b, 0);
  const locks = data?.daemon_locks || {};
  const fleet = data?.fleet_availability;

  return (
    <>
      <PanelHead title="System overview" sub="refreshes every 5s" />
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
        <h3>Daemon locks (which instance owns each singleton)</h3>
        <div className="card">
          <DataTable
            columns={[
              { key: "name", label: "Daemon" },
              {
                key: "holder", label: "Holder", mono: true,
                render: (r) => r.holder || <Pill tone="critical">no holder</Pill>,
              },
            ]}
            rows={Object.entries(locks).map(([name, holder]) => ({ name, holder }))}
            keyFn={(r) => r.name}
          />
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
  const { data, error } = usePoll(() => api("/admin/render-groups/active"), 5000);
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
  const { data, error } = usePoll(() => api("/admin/jobs/active"), 5000);
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

export function MachinesPanel() {
  const { data, error } = usePoll(() => api("/admin/machines"), 15000);
  const machines = data?.machines || [];
  return (
    <>
      <PanelHead title="Community machines" sub="refreshes every 15s" />
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No machines registered."
          columns={[
            { key: "id", label: "Machine", mono: true, render: (m) => shortId(m.id) },
            { key: "gpu_model", label: "GPU" },
            { key: "gpu_vram_gb", label: "VRAM", num: true, render: (m) => `${m.gpu_vram_gb ?? "–"} GB` },
            {
              key: "alive", label: "Liveness",
              render: (m) => (m.alive
                ? <Pill tone="good">alive · {fmtSecs(m.heartbeat_age_sec)}</Pill>
                : <Pill tone="critical">offline</Pill>),
            },
            { key: "status", label: "DB status", render: (m) => <Pill tone={statusTone(m.status)}>{m.status}</Pill> },
            { key: "redis_status", label: "Redis status", render: (m) => m.redis_status || "–" },
            { key: "machine_type", label: "OS" },
            { key: "user_id", label: "Owner", mono: true, render: (m) => shortId(m.user_id) },
            { key: "last_seen_at", label: "Last seen", render: (m) => fmtAgo(m.last_seen_at) },
          ]}
          rows={machines}
          keyFn={(m) => m.id}
        />
      </div>
    </>
  );
}

// ------------------------------------------------------------------- users

export function UsersPanel() {
  const [selected, setSelected] = useState(null);
  const { data, error } = usePoll(() => api("/admin/users"), 60000);
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
  const { data, error } = usePoll(() => api(`/admin/costs/summary?days=${days}`), 60000, [days]);
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
  const { data, error } = usePoll(() => api("/admin/failures/recent?limit=100"), 30000);
  return (
    <>
      <PanelHead title="Failure events" sub="most recent first · refreshes every 30s" />
      <ErrorBanner error={error} />
      <div className="card">
        <DataTable
          emptyText="No failures recorded. Nice."
          columns={[
            { key: "occurred_at", label: "When", render: (f) => fmtAgo(f.occurred_at) },
            { key: "provider", label: "Provider" },
            { key: "failure_type", label: "Type", render: (f) => <Pill tone="serious">{f.failure_type}</Pill> },
            { key: "job_id", label: "Job", mono: true, render: (f) => shortId(f.job_id) },
            { key: "group_id", label: "Group", mono: true, render: (f) => shortId(f.group_id) },
            { key: "action_taken", label: "Action" },
            { key: "resolved", label: "Resolved", render: (f) => (f.resolved ? <Pill tone="good">yes</Pill> : <Pill tone="warning">no</Pill>) },
            {
              key: "error_msg", label: "Error",
              render: (f) => <span title={f.error_msg}>{(f.error_msg || "").slice(0, 80)}</span>,
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
  const { data, error } = usePoll(() => api("/admin/downloads"), 60000);
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
