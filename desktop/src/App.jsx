import { useState, useCallback } from "react";
import Sidebar from "./components/common/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";
import CreateRenderPage from "./pages/CreateRenderPage";
import MyJobsPage from "./pages/MyJobsPage";
import DownloadsPage from "./pages/DownloadsPage";
import AvailableMachinesPage from "./pages/AvailableMachinesPage";
import ConfigurationPage from "./pages/ConfigurationPage";
import LoginPage from "./components/common/LoginPage";
import ToastViewport from "./components/common/ToastViewport";
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
  // When a render is submitted we navigate to My Jobs AND deep-link straight
  // into the new job's detail view.  This holds the group_id to pre-select;
  // any manual navigation clears it so a later visit lands on the list.
  const [pendingJobId, setPendingJobId] = useState(null);
  const [backendUrl, setBackendUrl] = useState(
    "https://pcrent-server-v2-930713698987.asia-northeast1.run.app"
  );

  const agent = useAgent(user && mode === "renter" ? backendUrl : null);
  const jobsHook = useJobs(user ? backendUrl : null);
  const { downloads } = useDownloads();
  const activeDownloadCount = Object.values(downloads).filter((d) => d.status === "loading").length;

  // Manual navigation (sidebar, in-page links) clears any pending deep-link
  // so the user lands on the page they asked for, not a stale job detail.
  const handleNavigate = useCallback((nextPage) => {
    setPendingJobId(null);
    setPage(nextPage);
  }, []);

  const handleModeChange = useCallback(
    (newMode) => {
      setMode(newMode);
      localStorage.setItem("pcrent_mode", newMode);
      setPendingJobId(null);
      setPage(DEFAULT_PAGES[newMode]);
    },
    []
  );

  const handleJobSubmitted = useCallback(
    (groupId, filename, tasks, totalFrames) => {
      jobsHook.addRenderGroup(groupId, filename, tasks, totalFrames);
      setPendingJobId(groupId);
      setPage("myjobs");
    },
    [jobsHook.addRenderGroup]
  );

  if (authLoading) return <div className="auth-loading">Loading...</div>;
  if (!user) return <LoginPage />;

  return (
    <div className="app">
      <ToastViewport />
      <Sidebar
        activePage={page}
        onNavigate={handleNavigate}
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
            ongoingJobs={jobsHook.ongoingJobs}
            pastJobs={jobsHook.pastJobs}
            loadingOngoing={jobsHook.loadingOngoing}
            loadingPast={jobsHook.loadingPast}
            loadingMoreOngoing={jobsHook.loadingMoreOngoing}
            loadingMorePast={jobsHook.loadingMorePast}
            hasMoreOngoing={jobsHook.hasMoreOngoing}
            hasMorePast={jobsHook.hasMorePast}
            loadMoreOngoing={jobsHook.loadMoreOngoing}
            loadMorePast={jobsHook.loadMorePast}
            removeJob={jobsHook.removeJob}
            backendUrl={backendUrl}
            markRenderGroupCancelled={jobsHook.markRenderGroupCancelled}
            updateGroup={jobsHook.updateGroup}
            onRefresh={jobsHook.refresh}
            onNavigate={handleNavigate}
            initialSelectedJobId={pendingJobId}
          />
        )}
        {page === "downloads" && (
          <DownloadsPage />
        )}
        {page === "available" && (
          <AvailableMachinesPage backendUrl={backendUrl} />
        )}

        {/* Shared pages */}
        {page === "configuration" && (
          <ConfigurationPage backendUrl={backendUrl} />
        )}
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
