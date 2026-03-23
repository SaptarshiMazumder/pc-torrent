import { useState } from "react";
import Sidebar from "./components/Sidebar";
import DashboardPage from "./pages/DashboardPage";
import LogsPage from "./pages/LogsPage";
import SettingsPage from "./pages/SettingsPage";
import { useAgent } from "./hooks/useAgent";

export default function App() {
  const [page, setPage] = useState("dashboard");
  const [backendUrl, setBackendUrl] = useState("https://pcrent-server-wbifmyiivq-an.a.run.app");
  const agent = useAgent();

  return (
    <div className="app">
      <Sidebar
        activePage={page}
        onNavigate={setPage}
        status={agent.status}
      />
      <main className="main-content">
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
