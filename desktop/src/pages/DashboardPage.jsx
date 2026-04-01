import GpuInfoCard from "../components/GpuInfoCard";
import ConnectButton from "../components/ConnectButton";
import StatusIndicator from "../components/StatusIndicator";
import JobCard from "../components/JobCard";
import RuntimeCard from "../components/RuntimeCard";

export default function DashboardPage({
  status,
  message,
  machineId,
  systemInfo,
  runtimeInfo,
  preflightSteps,
  currentJob,
  backendUrl,
}) {
  return (
    <div className="page dashboard-page">
      <h2>Dashboard</h2>

      <div className="connect-section">
        <ConnectButton status={status} backendUrl={backendUrl} runtimeInfo={runtimeInfo} />
        <StatusIndicator status={status} message={message} />
        {machineId && (
          <div className="machine-id">
            Machine ID: <code>{machineId}</code>
          </div>
        )}
      </div>

      <RuntimeCard runtimeInfo={runtimeInfo} preflightSteps={preflightSteps} status={status} />
      <GpuInfoCard systemInfo={systemInfo} />
      <JobCard currentJob={currentJob} status={status} />
    </div>
  );
}
