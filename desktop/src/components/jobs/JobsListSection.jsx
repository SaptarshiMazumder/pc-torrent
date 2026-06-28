import JobsTable from "./JobsTable";
import JobGrid from "./JobGrid";
import { JOB_TABLE_COLUMNS } from "./jobTableColumns";

// Renders ONE jobs section's body in the chosen view (table | grid) with its
// own load-more.  Applies the shared search + the section's sort (deriveRows)
// to both views so they stay consistent.  Grid keeps its infinite-scroll
// loader; table gets an explicit "Load more" button.
export default function JobsListSection({
  jobs,
  view,
  viewMode,
  search,
  onSelect,
  onRemove,
  backendUrl,
  authToken,
  hasMore,
  loadingMore,
  onLoadMore,
}) {
  const rows = view.deriveRows(jobs, search, JOB_TABLE_COLUMNS);

  if (viewMode === "grid") {
    return (
      <JobGrid
        jobs={rows}
        authToken={authToken}
        backendUrl={backendUrl}
        onSelect={onSelect}
        onRemove={onRemove}
        hasMore={hasMore}
        loadingMore={loadingMore}
        onLoadMore={onLoadMore}
      />
    );
  }

  return (
    <>
      <JobsTable
        jobs={rows}
        columns={JOB_TABLE_COLUMNS}
        sort={view.sort}
        onSort={view.toggleSort}
        onSelect={onSelect}
        backendUrl={backendUrl}
        authToken={authToken}
      />
      {hasMore && !loadingMore ? (
        <button
          type="button"
          className="btn btn-secondary jobs-load-more"
          onClick={onLoadMore}
        >
          Load more
        </button>
      ) : null}
    </>
  );
}
