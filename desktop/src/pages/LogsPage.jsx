import LogViewer from "../components/LogViewer";

export default function LogsPage({ logs }) {
  return (
    <div className="page logs-page">
      <div className="page-header">
        <h2>Logs</h2>
        <span className="log-count">{logs.length} entries</span>
      </div>
      <LogViewer logs={logs} />
    </div>
  );
}
