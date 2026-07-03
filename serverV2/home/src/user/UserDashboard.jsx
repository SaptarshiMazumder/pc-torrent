// Non-admin view: the signed-in user's own renders and account stats.
// Uses the same authed endpoints the desktop app calls.

import { api } from "../api.js";
import { fmtAgo, fmtCredits, fmtNum } from "../format.js";
import {
  DataTable, Pill, Progress, Tile, statusTone, usePoll,
} from "../components/ui.jsx";

export default function UserDashboard({ profile }) {
  const active = usePoll(
    () => api("/render-groups?status_group=active&limit=50"), 5000,
  );
  const past = usePoll(
    () => api("/render-groups?status_group=terminal&limit=50"), 30000,
  );

  const activeGroups = active.data?.groups || [];
  const pastGroups = past.data?.groups || [];

  return (
    <main className="main">
      <div className="panel-head"><h2>Your dashboard</h2></div>
      <div className="tiles">
        <Tile label="Tokens" value={fmtCredits(profile?.credits)} />
        <Tile label="Tier" value={profile?.tier || "free"} />
        <Tile label="Active renders" value={fmtNum(activeGroups.length)} />
        <Tile
          label="Recent finished"
          value={fmtNum(pastGroups.length)}
          hint={past.data?.has_more ? "showing latest 50" : undefined}
        />
      </div>

      <div className="section">
        <h3>Active renders</h3>
        <div className="card">
          <DataTable
            emptyText="Nothing rendering right now."
            columns={[
              { key: "input_filename", label: "File" },
              { key: "status", label: "Status", render: (g) => <Pill tone={statusTone(g.status)}>{g.status}</Pill> },
              { key: "tier", label: "Tier" },
              { key: "progress", label: "Progress", render: (g) => <Progress pct={g.overall_progress_pct} /> },
              {
                key: "frames", label: "Frames", num: true,
                render: (g) => `${fmtNum(g.overall_rendered_frames)} / ${fmtNum(g.total_frames)}`,
              },
              {
                key: "tokens", label: "Tokens (actual / est)", num: true,
                render: (g) => `${fmtCredits(g.total_actual_cost_credits)} / ${fmtCredits(g.total_estimated_cost_credits)}`,
              },
              { key: "submitted_at", label: "Submitted", render: (g) => fmtAgo(g.submitted_at) },
            ]}
            rows={activeGroups}
            keyFn={(g) => g.group_id || g.id}
          />
        </div>
      </div>

      <div className="section">
        <h3>History</h3>
        <div className="card">
          <DataTable
            emptyText="No finished renders yet."
            columns={[
              { key: "input_filename", label: "File" },
              { key: "status", label: "Status", render: (g) => <Pill tone={statusTone(g.status)}>{g.status}</Pill> },
              {
                key: "frames", label: "Frames", num: true,
                render: (g) => `${fmtNum(g.overall_rendered_frames)} / ${fmtNum(g.total_frames)}`,
              },
              {
                key: "tokens", label: "Tokens", num: true,
                render: (g) => fmtCredits(g.total_actual_cost_credits),
              },
              { key: "completed_at", label: "Finished", render: (g) => fmtAgo(g.completed_at || g.submitted_at) },
            ]}
            rows={pastGroups}
            keyFn={(g) => g.group_id || g.id}
          />
        </div>
      </div>
    </main>
  );
}
