import GpuInfoCard from "../components/GpuInfoCard";
import ConnectButton from "../components/ConnectButton";
import StatusIndicator from "../components/StatusIndicator";
import JobCard from "../components/JobCard";

export default function DashboardPage({
  status,
  message,
  machineId,
  systemInfo,
  currentJob,
  backendUrl,
}) {
  return (
    <div className="page dashboard-page">
      <h2>Dashboard</h2>

      <GpuInfoCard systemInfo={systemInfo} />

      <div className="connect-section">
        <ConnectButton status={status} backendUrl={backendUrl} />
        <StatusIndicator status={status} message={message} />
        {machineId && (
          <div className="machine-id">
            Machine ID: <code>{machineId}</code>
          </div>
        )}
      </div>

      <JobCard currentJob={currentJob} status={status} />
    </div>
  );
}
