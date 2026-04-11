import JobGridCard from "./JobGridCard";
import { jobKey } from "../../utils/jobUtils";

export default function JobGrid({ jobs, authToken, backendUrl, onSelect, onRemove }) {
  return (
    <div className="job-grid">
      {jobs.map((job) => {
        const id = jobKey(job);
        if (!id) return null;
        return (
          <JobGridCard
            key={id}
            job={job}
            authToken={authToken}
            backendUrl={backendUrl}
            onClick={onSelect}
            onRemove={onRemove}
          />
        );
      })}
    </div>
  );
}
