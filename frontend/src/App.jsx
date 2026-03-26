import { useState, useEffect, useRef } from "react";
import { getMachines, submitJob, getJob, downloadUrl } from "./api";
import "./App.css";

// -----------------------------------------------
// Page: Browse Machines
// -----------------------------------------------
function MachinesPage({ onRent }) {
  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = () => {
    setLoading(true);
    getMachines()
      .then(setMachines)
      .catch(() => setError("Cannot reach backend"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); }, []);

  if (loading) return <p className="status">Loading machines...</p>;
  if (error) return <p className="error">{error}</p>;

  return (
    <div>
      <div className="page-header">
        <h2>Available Machines</h2>
        <button className="btn-secondary" onClick={load}>Refresh</button>
      </div>
      {machines.length === 0 ? (
        <p className="status">No machines available right now.</p>
      ) : (
        <table className="machines-table">
          <thead>
            <tr>
              <th>GPU</th>
              <th>VRAM</th>
              <th>CPU Cores</th>
              <th>RAM</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {machines.map(m => (
              <tr key={m.id}>
                <td>{m.gpu_model}</td>
                <td>{m.gpu_vram_gb} GB</td>
                <td>{m.cpu_cores}</td>
                <td>{m.ram_gb} GB</td>
                <td>
                  <button className="btn-primary" onClick={() => onRent(m)}>
                    Rent
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

// -----------------------------------------------
// Page: Submit Job
// -----------------------------------------------
function SubmitJobPage({ machine, onBack, onSubmitted }) {
  const [file, setFile] = useState(null);
  const [loading, setLoading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) return setError("Select a .blend file or .zip project bundle first");
    setLoading(true);
    setError(null);
    setProgress(0);
    try {
      const job = await submitJob(machine.id, file, setProgress);
      onSubmitted(job.job_id);
    } catch (err) {
      setError(err.message);
      setLoading(false);
    }
  };

  return (
    <div>
      <button className="btn-back" onClick={onBack}>← Back</button>
      <h2>Submit Render Job</h2>
      <div className="machine-card">
        <strong>{machine.gpu_model}</strong>
        <span>{machine.gpu_vram_gb} GB VRAM · {machine.cpu_cores} cores · {machine.ram_gb} GB RAM</span>
      </div>
      <form onSubmit={handleSubmit} className="submit-form">
        <label>
          Project File (.blend or .zip)
          <input
            type="file"
            accept=".blend,.zip"
            onChange={e => setFile(e.target.files[0])}
          />
        </label>
        <p className="status">
          Use a single `.blend` only if textures and linked libraries are packed into it.
          Otherwise upload a `.zip` with the full project folder.
        </p>
        {file && <p className="file-name">{file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)</p>}
        {error && <p className="error">{error}</p>}
        {loading && (
          <div className="progress-bar-wrap">
            <div className="progress-bar" style={{ width: `${progress}%` }} />
            <span className="progress-text">Uploading... {progress}%</span>
          </div>
        )}
        <button className="btn-primary" type="submit" disabled={loading}>
          {loading ? `Uploading... ${progress}%` : "Start Render"}
        </button>
      </form>
    </div>
  );
}

// -----------------------------------------------
// Page: Job Status
// -----------------------------------------------
const STATUS_LABEL = {
  pending: "Waiting for machine...",
  running: "Rendering...",
  done: "Done!",
  failed: "Failed"
};

function JobStatusPage({ jobId, onBack }) {
  const [job, setJob] = useState(null);
  const [error, setError] = useState(null);
  const intervalRef = useRef();

  const fetchJob = async () => {
    try {
      const j = await getJob(jobId);
      setJob(j);
      if (j.status === "done" || j.status === "failed") {
        clearInterval(intervalRef.current);
      }
    } catch {
      setError("Failed to fetch job status");
    }
  };

  useEffect(() => {
    fetchJob();
    intervalRef.current = setInterval(fetchJob, 3000);
    return () => clearInterval(intervalRef.current);
  }, [jobId]);

  if (error) return <p className="error">{error}</p>;
  if (!job) return <p className="status">Loading...</p>;

  const statusLabel = job.status === "done" && job.error
    ? "Done with warnings"
    : (STATUS_LABEL[job.status] || job.status);
  const totalFrames =
    typeof job.total_frames === "number" ? job.total_frames : null;
  const renderedFrames =
    typeof job.rendered_frames === "number" ? job.rendered_frames : 0;
  const progressPct =
    typeof job.progress_pct === "number"
      ? Math.max(0, Math.min(100, job.progress_pct))
      : null;
  const hasTotalFrames = typeof totalFrames === "number" && totalFrames > 0;
  const showRenderProgress =
    job.status === "running" ||
    hasTotalFrames ||
    renderedFrames > 0;

  return (
    <div>
      <button className="btn-back" onClick={onBack}>← Browse Machines</button>
      <h2>Job Status</h2>
      <div className="job-card">
        <div className="job-id">Job ID: <code>{jobId}</code></div>
        <div className={`status-badge status-${job.status}`}>{statusLabel}</div>

        {(job.status === "pending" || job.status === "running") && (
          <div className="spinner-wrap">
            <div className="spinner" />
            <p>{job.status === "pending" ? "Waiting for machine to pick up job..." : "Rendering in progress..."}</p>
          </div>
        )}

        {showRenderProgress && (
          <div className="render-progress-wrap">
            <div className={`render-progress-track ${progressPct === null ? "indeterminate" : ""}`}>
              <div
                className="render-progress-fill"
                style={{ width: `${progressPct ?? 100}%` }}
              />
            </div>
            <div className="render-progress-meta">
              <span>
                {hasTotalFrames
                  ? `${Math.min(renderedFrames, totalFrames)} / ${totalFrames} frames rendered`
                  : "Preparing render..."}
              </span>
              <span>{progressPct !== null ? `${Math.round(progressPct)}%` : "Working..."}</span>
            </div>
          </div>
        )}

        {job.status === "failed" && (
          <div className="error">
            <strong>Error:</strong> {job.error || "Unknown error"}
          </div>
        )}

        {job.status === "done" && (
          <div className="done-section">
            <p>Rendered <strong>{job.output_files.length}</strong> file(s)</p>
            {job.error && (
              <p className="status">
                <strong>Warning:</strong> {job.error}
              </p>
            )}
            <a className="btn-primary" href={downloadUrl(jobId)} download>
              Download Output
            </a>
            {job.output_files.length > 0 && (
              <ul className="file-list">
                {job.output_files.map(f => <li key={f}>{f}</li>)}
              </ul>
            )}
          </div>
        )}

        <div className="job-meta">
          <span>Submitted: {new Date(job.submitted_at).toLocaleString()}</span>
          {job.completed_at && (
            <span>Completed: {new Date(job.completed_at).toLocaleString()}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// -----------------------------------------------
// App Shell
// -----------------------------------------------
export default function App() {
  const [page, setPage] = useState("machines");
  const [selectedMachine, setSelectedMachine] = useState(null);
  const [jobId, setJobId] = useState(null);

  const handleRent = (machine) => {
    setSelectedMachine(machine);
    setPage("submit");
  };

  const handleSubmitted = (id) => {
    setJobId(id);
    setPage("status");
  };

  return (
    <div className="app">
      <header>
        <h1 onClick={() => setPage("machines")} style={{ cursor: "pointer" }}>
          PC Rent
        </h1>
        <span className="tagline">Distributed rendering marketplace</span>
      </header>
      <main>
        {page === "machines" && <MachinesPage onRent={handleRent} />}
        {page === "submit" && selectedMachine && (
          <SubmitJobPage
            machine={selectedMachine}
            onBack={() => setPage("machines")}
            onSubmitted={handleSubmitted}
          />
        )}
        {page === "status" && jobId && (
          <JobStatusPage jobId={jobId} onBack={() => setPage("machines")} />
        )}
      </main>
    </div>
  );
}
