import {
  resolveJobFilename,
  statusRank,
  jobProgressPct,
  jobTotalFrames,
  jobGpuCount,
  jobPixels,
  jobResolutionLabel,
  jobSamples,
  jobEngineLabel,
  jobOutputFormat,
  jobOutputLabel,
  jobFileSizeBytes,
  jobSubmittedMs,
  jobDurationSec,
  formatBytesLabel,
  formatDurationLabel,
  formatDateTimeLabel,
  jobCostCredits,
  compareJobCost,
} from "../../utils/jobUtils";
import { formatCredits } from "../../utils/creditsFormat";
import JobNameCell from "./JobNameCell";
import JobStatusBadge from "./JobStatusBadge";
import JobProgressCell from "./JobProgressCell";

// Declarative column set -- the single source of truth for the jobs table.
// Each column owns how it SORTS (sortValue: job -> primitive) and how it
// RENDERS (Cell).  JobsTable / JobTableRow iterate this and stay completely
// field-agnostic, so adding a column is one entry here and nothing else
// changes.  The Aurora Glass redesign restyles this table (glass panel, mono
// headers, KPI strip, filter tabs) but keeps every data column intact.
export const JOB_TABLE_COLUMNS = [
  {
    key: "name",
    label: "Job",
    sortable: true,
    align: "left",
    sortValue: (job) => resolveJobFilename(job).toLowerCase(),
    Cell: ({ job, backendUrl, authToken }) => (
      <JobNameCell job={job} backendUrl={backendUrl} authToken={authToken} />
    ),
  },
  {
    key: "status",
    label: "Status",
    sortable: true,
    align: "left",
    sortValue: (job) => statusRank(job?.status),
    Cell: ({ job }) => <JobStatusBadge status={job?.status} />,
  },
  {
    key: "progress",
    label: "Progress",
    sortable: true,
    align: "left",
    sortValue: (job) => jobProgressPct(job),
    Cell: ({ job }) => <JobProgressCell job={job} />,
  },
  {
    key: "frames",
    label: "Frames",
    sortable: true,
    align: "right",
    sortValue: (job) => jobTotalFrames(job),
    Cell: ({ job }) => jobTotalFrames(job) || "—",
  },
  {
    key: "gpus",
    label: "GPUs",
    sortable: true,
    align: "right",
    sortValue: (job) => jobGpuCount(job),
    Cell: ({ job }) => jobGpuCount(job) || "—",
  },
  {
    key: "resolution",
    label: "Resolution",
    sortable: true,
    align: "right",
    sortValue: (job) => jobPixels(job),
    Cell: ({ job }) => jobResolutionLabel(job),
  },
  {
    key: "samples",
    label: "Samples",
    sortable: true,
    align: "right",
    sortValue: (job) => jobSamples(job) ?? -1,
    Cell: ({ job }) => (jobSamples(job) ?? "—"),
  },
  {
    key: "engine",
    label: "Engine",
    sortable: true,
    align: "left",
    sortValue: (job) => jobEngineLabel(job),
    Cell: ({ job }) => jobEngineLabel(job),
  },
  {
    key: "output",
    label: "Output",
    sortable: true,
    align: "left",
    sortValue: (job) => jobOutputFormat(job),
    Cell: ({ job }) => jobOutputLabel(job),
  },
  {
    key: "size",
    label: "Scene",
    sortable: true,
    align: "right",
    sortValue: (job) => jobFileSizeBytes(job),
    Cell: ({ job }) => formatBytesLabel(jobFileSizeBytes(job)),
  },
  {
    key: "submitted",
    label: "Submitted",
    sortable: true,
    align: "left",
    sortValue: (job) => jobSubmittedMs(job),
    Cell: ({ job }) => formatDateTimeLabel(job?.submitted_at),
  },
  {
    key: "duration",
    label: "Duration",
    sortable: true,
    align: "right",
    sortValue: (job) => jobDurationSec(job),
    Cell: ({ job }) => formatDurationLabel(jobDurationSec(job)),
  },
  {
    key: "cost",
    label: "Cost",
    sortable: true,
    align: "right",
    sortValue: (job) => jobCostCredits(job),
    // Custom tiered order: in-progress (no final cost) top, finished-with-
    // cost in the middle by direction, null-cost finished jobs at the bottom.
    comparator: (a, b, dir) => compareJobCost(a, b, dir),
    Cell: ({ job }) => {
      const c = jobCostCredits(job);
      return c > 0 ? formatCredits(c) : "—";
    },
  },
];
