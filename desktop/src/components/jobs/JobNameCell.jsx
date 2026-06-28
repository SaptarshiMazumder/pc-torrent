import { resolveJobFilename, jobOutputLabel } from "../../utils/jobUtils";
import JobThumbnail from "./JobThumbnail";

// Name cell: small thumbnail (reuses JobThumbnail -> shows the EXR icon for
// .exr output) + filename + an output-format tag (PNG / EXR).
export default function JobNameCell({ job, backendUrl, authToken }) {
  const name = resolveJobFilename(job);
  const output = jobOutputLabel(job);
  return (
    <div className="job-name-cell">
      <div className="job-name-thumb">
        <JobThumbnail job={job} authToken={authToken} backendUrl={backendUrl} />
      </div>
      <div className="job-name-text">
        <span className="job-name-title" title={name}>{name}</span>
        {output !== "—" && <span className="job-name-tag">{output}</span>}
      </div>
    </div>
  );
}
