import { lazy, Suspense, useState, useCallback } from "react";
import Sidebar from "./components/common/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import CreateRenderPage from "./pages/CreateRenderPage";
import MyJobsPage from "./pages/MyJobsPage";
import TelemetryPage from "./pages/TelemetryPage";
import DownloadsPage from "./pages/DownloadsPage";
import AvailableMachinesPage from "./pages/AvailableMachinesPage";
import ConfigurationPage from "./pages/ConfigurationPage";
import AboutPage from "./pages/AboutPage";
import LoginPage from "./components/common/LoginPage";
import UpdateRequiredModal from "./components/common/UpdateRequiredModal";
import ToastViewport from "./components/common/ToastViewport";
import UserCreditsCorner from "./components/profile/UserCreditsCorner";
import { useAgent } from "./hooks/useAgent";
import { useJobs } from "./hooks/useJobs";
import { useVersionGate } from "./hooks/useVersionGate";
import { useAuth } from "./contexts/AuthContext";
import { useUserProfile } from "./contexts/UserProfileContext";
import { useDownloads } from "./contexts/DownloadContext";

// Cinematic 3D backdrop is lazy-loaded so three.js stays out of the main bundle
// and the app paints instantly; the scene fades in a beat later.
const ForgeSceneBackdrop = lazy(() => import("./components/scene/ForgeSceneBackdrop"));

const DEFAULT_PAGES = { renter: "dashboard", rentee: "create" };

export default function App() {
  const { user, loading: authLoading } = useAuth();
  const { profile } = useUserProfile();
  const isAdmin = profile?.role === "admin";
  const [mode, setMode] = useState(
    () => localStorage.getItem("pcrent_mode") || "rentee"
  );
  const [page, setPage] = useState(() => DEFAULT_PAGES[localStorage.getItem("pcrent_mode") || "rentee"] || "create");
  const backendUrl = import.meta.env.VITE_BACKEND_URL;

  // Boot-time force-update gate.  Runs BEFORE auth so an out-of-date
  // client can't even reach the login screen.  Network/backend errors
  // resolve to "ok" so a server hiccup doesn't lock everyone out.
  const versionGate = useVersionGate(backendUrl);

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

  // Gating logic returns the foreground content; the 3D backdrop is mounted
  // separately below so it persists unchanged across every gate transition
  // (loading → login → app) and the WebGL context is created only once.
  function renderContent() {
    if (versionGate.status === "checking") return <div className="auth-loading">Loading...</div>;
    if (versionGate.status === "blocked") {
      return (
        <UpdateRequiredModal
          currentVersion={versionGate.currentVersion}
          minVersion={versionGate.minVersion}
          latestUrl={versionGate.latestUrl}
        />
      );
    }
    if (authLoading) return <div className="auth-loading">Loading...</div>;
    if (!user) return <LoginPage />;
    return renderApp();
  }

  function renderApp() {
    return (
    <div className="app">
      <ToastViewport />
      <UserCreditsCorner />
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
            onNavigate={setPage}
          />
        )}
        {page === "stats" && (
          <TelemetryPage
            ongoingJobs={jobsHook.ongoingJobs}
            pastJobs={jobsHook.pastJobs}
            hasMorePast={jobsHook.hasMorePast}
            loadingMorePast={jobsHook.loadingMorePast}
            loadMorePast={jobsHook.loadMorePast}
          />
        )}
        {page === "downloads" && (
          <DownloadsPage />
        )}
        {page === "available" && (
          <AvailableMachinesPage backendUrl={backendUrl} />
        )}

        {/* Shared pages */}
        {page === "configuration" && isAdmin && (
          <ConfigurationPage backendUrl={backendUrl} />
        )}
        {page === "about" && (
          <AboutPage />
        )}
      </main>
    </div>
    );
  }

  return (
    <>
      <Suspense fallback={null}>
        <ForgeSceneBackdrop mode={user ? "ambient" : "hero"} />
      </Suspense>
      {renderContent()}
    </>
  );
}
