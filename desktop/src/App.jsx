import { useState, useCallback } from "react";
import Sidebar from "./components/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";
import MarketplacePage from "./pages/MarketplacePage";
import MyJobsPage from "./pages/MyJobsPage";
import { useAgent } from "./hooks/useAgent";
import { useJobs } from "./hooks/useJobs";

const DEFAULT_PAGES = { renter: "dashboard", rentee: "marketplace" };

export default function App() {
  const [mode, setMode] = useState(
    () => localStorage.getItem("pcrent_mode") || "rentee"
  );
  const [page, setPage] = useState(DEFAULT_PAGES[mode] || "marketplace");
  const [backendUrl, setBackendUrl] = useState(
    "https://pcrent-server-wbifmyiivq-an.a.run.app"
  );

  const agent = useAgent();
  const jobsHook = useJobs(backendUrl);

  const handleModeChange = useCallback(
    (newMode) => {
      setMode(newMode);
      localStorage.setItem("pcrent_mode", newMode);
      setPage(DEFAULT_PAGES[newMode]);
    },
    []
  );

  const handleJobSubmitted = useCallback(
    (jobId, machineGpu, filename) => {
      jobsHook.addJob(jobId, machineGpu, filename);
      setPage("myjobs");
    },
    [jobsHook.addJob]
  );

  return (
    <div className="app">
      <Sidebar
        activePage={page}
        onNavigate={setPage}
        status={agent.status}
        mode={mode}
        onModeChange={handleModeChange}
      />
      <main className="main-content">
        {/* Renter pages */}
        {page === "dashboard" && (
          <DashboardPage
            status={agent.status}
            message={agent.message}
            machineId={agent.machineId}
            systemInfo={agent.systemInfo}
            runtimeInfo={agent.runtimeInfo}
            currentJob={agent.currentJob}
            backendUrl={backendUrl}
          />
        )}
        {page === "logs" && <LogsPage logs={agent.logs} />}

        {/* Rentee pages */}
        {page === "marketplace" && (
          <MarketplacePage
            backendUrl={backendUrl}
            onJobSubmitted={handleJobSubmitted}
          />
        )}
        {page === "myjobs" && (
          <MyJobsPage
            jobs={jobsHook.jobs}
            removeJob={jobsHook.removeJob}
            backendUrl={backendUrl}
          />
        )}

        {/* Shared pages */}
        {page === "settings" && (
          <SettingsPage
            backendUrl={backendUrl}
            onBackendUrlChange={setBackendUrl}
          />
        )}
      </main>
    </div>
  );
}
