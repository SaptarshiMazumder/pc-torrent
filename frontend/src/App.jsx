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
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!file) return setError("Select a .blend file first");
    setLoading(true);
    setError(null);
    try {
      const job = await submitJob(machine.id, file);
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
          Blender File (.blend)
          <input
            type="file"
            accept=".blend"
            onChange={e => setFile(e.target.files[0])}
          />
        </label>
        {file && <p className="file-name">{file.name} ({(file.size / 1024 / 1024).toFixed(1)} MB)</p>}
        {error && <p className="error">{error}</p>}
        <button className="btn-primary" type="submit" disabled={loading}>
          {loading ? "Submitting..." : "Start Render"}
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

  return (
    <div>
      <button className="btn-back" onClick={onBack}>← Browse Machines</button>
      <h2>Job Status</h2>
      <div className="job-card">
        <div className="job-id">Job ID: <code>{jobId}</code></div>
        <div className={`status-badge status-${job.status}`}>{STATUS_LABEL[job.status] || job.status}</div>

        {(job.status === "pending" || job.status === "running") && (
          <div className="spinner-wrap">
            <div className="spinner" />
            <p>{job.status === "pending" ? "Waiting for machine to pick up job..." : "Rendering in progress..."}</p>
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
