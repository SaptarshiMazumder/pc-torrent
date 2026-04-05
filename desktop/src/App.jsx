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

  const agent = useAgent(mode === "renter" ? backendUrl : null);
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
    (groupId, filename, tasks, totalFrames) => {
      jobsHook.addRenderGroup(groupId, filename, tasks, totalFrames);
      setPage("myjobs");
    },
    [jobsHook.addRenderGroup]
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
            preflightSteps={agent.preflightSteps}
            currentJob={agent.currentJob}
            backendUrl={backendUrl}
          />
        )}
        {page === "logs" && (
          <LogsPage
            logs={agent.logs}
            onClearLogs={agent.clearLogs}
          />
        )}

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
            markRenderGroupCancelled={jobsHook.markRenderGroupCancelled}
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
