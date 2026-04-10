import { getGroupPreviewUrl, getSingleJobPreviewUrl } from "../../utils/jobUtils";

export default function JobThumbnail({ job, authToken, backendUrl, className }) {
  const previewUrl = job?.group_id
    ? getGroupPreviewUrl(job, backendUrl, authToken)
    : getSingleJobPreviewUrl(job, backendUrl, authToken);

  if (!previewUrl) {
    return (
      <div className={`job-thumb-placeholder ${className || ""}`}>
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <rect x="2" y="3" width="20" height="14" rx="2" />
          <path d="M8 21h8M12 17v4" />
        </svg>
      </div>
    );
  }

  return (
    <img
      className={`job-thumb-img ${className || ""}`}
      src={previewUrl}
      alt="Render preview"
      loading="lazy"
    />
  );
}
