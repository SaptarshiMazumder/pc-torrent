import { useEffect, useState } from "react";
import {
  CostsPanel, DownloadsPanel, FailuresPanel, JobsPanel, LiveRendersPanel,
  LogsPanel, MachinesPanel, OverviewPanel, UsersPanel,
} from "./panels.jsx";

const TABS = [
  { id: "overview", label: "Overview", el: OverviewPanel },
  { id: "live", label: "Live renders", el: LiveRendersPanel },
  { id: "jobs", label: "Jobs", el: JobsPanel },
  { id: "machines", label: "Machines", el: MachinesPanel },
  { id: "users", label: "Users", el: UsersPanel },
  { id: "costs", label: "Costs", el: CostsPanel },
  { id: "failures", label: "Failures", el: FailuresPanel },
  { id: "logs", label: "Logs", el: LogsPanel },
  { id: "downloads", label: "Downloads", el: DownloadsPanel },
];

function tabFromHash() {
  const hash = window.location.hash.replace(/^#\/?/, "");
  return TABS.some((t) => t.id === hash) ? hash : "overview";
}

export default function AdminDashboard() {
  const [tab, setTab] = useState(tabFromHash);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const Active = TABS.find((t) => t.id === tab)?.el || OverviewPanel;
  return (
    <>
      <nav className="tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`tab${t.id === tab ? " active" : ""}`}
            onClick={() => { window.location.hash = `/${t.id}`; }}
          >
            {t.label}
          </button>
        ))}
      </nav>
      <main className="main">
        <Active />
      </main>
    </>
  );
}
