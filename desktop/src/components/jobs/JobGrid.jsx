import { useEffect, useRef } from "react";
import JobGridCard from "./JobGridCard";
import { jobKey } from "../../utils/jobUtils";

export default function JobGrid({
  jobs,
  authToken,
  backendUrl,
  onSelect,
  onRemove,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
}) {
  const sentinelRef = useRef(null);

  useEffect(() => {
    if (!hasMore || loadingMore || !onLoadMore) return;
    const el = sentinelRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadMore();
      },
      { rootMargin: "0px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasMore, loadingMore, onLoadMore]);

  return (
    <>
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
      <div ref={sentinelRef} aria-hidden="true" style={{ height: 1 }} />
    </>
  );
}
