import { useState, useCallback } from "react";
import Sidebar from "./components/common/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";
import CreateRenderPage from "./pages/CreateRenderPage";
import MyJobsPage from "./pages/MyJobsPage";
import DownloadsPage from "./pages/DownloadsPage";
import AvailableMachinesPage from "./pages/AvailableMachinesPage";
import LoginPage from "./components/common/LoginPage";
import { useAgent } from "./hooks/useAgent";
import { useJobs } from "./hooks/useJobs";
import { useAuth } from "./contexts/AuthContext";
import { useDownloads } from "./contexts/DownloadContext";

const DEFAULT_PAGES = { renter: "dashboard", rentee: "create" };

export default function App() {
  const { user, loading: authLoading } = useAuth();
  const [mode, setMode] = useState(
    () => localStorage.getItem("pcrent_mode") || "rentee"
  );
  const [page, setPage] = useState(() => DEFAULT_PAGES[localStorage.getItem("pcrent_mode") || "rentee"] || "create");
  const [backendUrl, setBackendUrl] = useState(
    "https://pcrent-server-v2-930713698987.asia-northeast1.run.app"
  );

  const agent = useAgent(user && mode === "renter" ? backendUrl : null);
  const jobsHook = useJobs(user ? backendUrl : null);
  const { downloads } = useDownloads();
  const activeDownloadCount = Object.values(downloads).filter((d) => d.status === "loading").length;

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

  if (authLoading) return <div className="auth-loading">Loading...</div>;
  if (!user) return <LoginPage />;

  return (
    <div className="app">
      <Sidebar
        activePage={page}
        onNavigate={setPage}
        status={agent.status}
        mode={mode}
        onModeChange={handleModeChange}
        activeDownloadCount={activeDownloadCount}
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
        <div style={{ display: page === "create" ? "contents" : "none" }}>
          <CreateRenderPage
            backendUrl={backendUrl}
            onJobSubmitted={handleJobSubmitted}
          />
        </div>
        {page === "myjobs" && (
          <MyJobsPage
            jobs={jobsHook.jobs}
            loading={jobsHook.loading}
            loadingMore={jobsHook.loadingMore}
            removeJob={jobsHook.removeJob}
            backendUrl={backendUrl}
            markRenderGroupCancelled={jobsHook.markRenderGroupCancelled}
            updateGroup={jobsHook.updateGroup}
            onRefresh={jobsHook.refresh}
            onNavigate={setPage}
          />
        )}
        {page === "downloads" && (
          <DownloadsPage />
        )}
        {page === "available" && (
          <AvailableMachinesPage backendUrl={backendUrl} />
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
