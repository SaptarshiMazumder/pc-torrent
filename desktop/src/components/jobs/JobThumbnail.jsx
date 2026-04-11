import { getGroupPreviewUrl, getSingleJobPreviewUrl } from "../../utils/jobUtils";

function BlenderLogo() {
  return (
    <svg width="48" height="48" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path
        d="M53.3 82.6c-18.6 0-33.7-14.1-33.7-31.5S34.7 19.6 53.3 19.6c7.5 0 14.4 2.3 20 6.3L42 50.7l31.4 25c-5.6 4.2-12.7 6.9-20.1 6.9z"
        fill="url(#blender_grad)"
      />
      <path
        d="M73.4 25.9C67 20.6 59 17.4 50.3 17.4c-9.7 0-18.4 3.9-24.8 10.1L8.9 41.3c-1.2 1-0.5 3 1.1 3H27l-15.6 12.6c-1.2 1-0.4 3 1.2 3h13.2"
        stroke="url(#blender_stroke)" strokeWidth="2.5" strokeLinecap="round" fill="none"
      />
      <ellipse cx="55" cy="51" rx="16" ry="15.5" fill="#161833" stroke="rgba(180,175,220,0.25)" strokeWidth="1.5"/>
      <ellipse cx="55" cy="51" rx="8" ry="7.8" fill="url(#blender_inner)"/>
      <defs>
        <linearGradient id="blender_grad" x1="20" y1="20" x2="80" y2="80" gradientUnits="userSpaceOnUse">
          <stop stopColor="#e8724a" />
          <stop offset="1" stopColor="#5ea0fa" />
        </linearGradient>
        <linearGradient id="blender_stroke" x1="8" y1="20" x2="60" y2="60" gradientUnits="userSpaceOnUse">
          <stop stopColor="#f5a623" />
          <stop offset="1" stopColor="#e8724a" />
        </linearGradient>
        <radialGradient id="blender_inner" cx="55" cy="49" r="8" gradientUnits="userSpaceOnUse">
          <stop stopColor="#f5a623" />
          <stop offset="1" stopColor="#e8724a" />
        </radialGradient>
      </defs>
    </svg>
  );
}

export default function JobThumbnail({ job, authToken, backendUrl, className }) {
  const previewUrl = job?.group_id
    ? getGroupPreviewUrl(job, backendUrl, authToken)
    : getSingleJobPreviewUrl(job, backendUrl, authToken);

  if (!previewUrl) {
    return (
      <div className={`job-thumb-placeholder ${className || ""}`}>
        <BlenderLogo />
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
