import i18n from "../i18n/i18n";

// Display label for a job status.  The status codes are the source of
// truth; the human label is looked up per-render so it follows the
// active language.  Unknown codes fall back to the raw status.
export function jobStatusLabel(status) {
  return i18n.t(`myJobs:status.${status}`, { defaultValue: status });
}

export const TERMINAL_STATUSES = new Set(["done", "cancelled", "failed"]);

export function isTerminalStatus(status) {
  return TERMINAL_STATUSES.has(status);
}

// Extensions the gallery can thumbnail (server PIL-decodes -> webp, browser
// renders).  Multilayer .exr (and other non-raster outputs) are NOT in this
// set, so the gallery shows a format badge instead of a broken/blank tile.
const PREVIEWABLE_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif", "bmp"]);

export function isPreviewableExtension(filename) {
  if (typeof filename !== "string") return false;
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return false;
  return PREVIEWABLE_EXTS.has(filename.slice(dot + 1).toLowerCase());
}

export function terminalFallbackPct(status) {
  if (status === "done") return 100;
  if (status === "cancelled" || status === "failed") return 0;
  return null;
}

export function resolveJobFilename(job) {
  if (typeof job?.filename === "string" && job.filename.trim()) return job.filename.trim();
  if (typeof job?.input_filename === "string" && job.input_filename.trim()) return job.input_filename.trim();
  return i18n.t("myJobs:untitled");
}

export function buildDownloadFolderName(jobFilename, id) {
  const rawName = typeof jobFilename === "string" && jobFilename.trim() ? jobFilename.trim() : "render";
  const baseName = rawName.replace(/\.[^/.]+$/, "");
  const suffix = typeof id === "string" && id ? id.slice(0, 8) : "job";
  return `render__${baseName}__${suffix}`;
}

// Derive a "type" sub-folder name from an output filename so the
// downloader groups same-type files together on disk.  Worker naming
// convention is ``<Type>_frame####.<ext>`` (main camera render) or
// ``<NodeName>_<SlotIdx>_frame####.<ext>`` (File Output node slot).
// Anything before the ``_frame####.<ext>`` suffix becomes the folder.
// Fallback for names that don't match the pattern: group by extension.
export function extractTypeFolder(filename) {
  if (typeof filename !== "string" || !filename) return "other";
  const m = filename.match(/^(.+?)_?frame\d+\.\w+$/i);
  if (m && m[1]) return m[1];
  const dot = filename.lastIndexOf(".");
  return dot > 0 ? filename.slice(dot + 1).toUpperCase() : "other";
}

// Count distinct frame numbers across a set of output files.  Used by
// the "Rendered Frames N (M)" header to express both counts in
// frame-units (otherwise N is frames but M ends up being raw file
// count, which is misleading when bonus File Output nodes inflate M).
export function countUniqueFrames(files) {
  if (!Array.isArray(files) || files.length === 0) return 0;
  const frames = new Set();
  for (const file of files) {
    const m = file?.filename?.match?.(/frame(\d+)\./);
    if (m) frames.add(parseInt(m[1], 10));
  }
  return frames.size;
}

export function frameIndexFromFilename(filename) {
  if (typeof filename !== "string") return -1;
  const match = filename.match(/(\d+)(?=\.[^.]+$)/);
  if (!match) return -1;
  const parsed = Number.parseInt(match[1], 10);
  return Number.isFinite(parsed) ? parsed : -1;
}

export function outputSort(a, b) {
  const frameA = frameIndexFromFilename(a?.filename || "");
  const frameB = frameIndexFromFilename(b?.filename || "");
  if (frameA !== frameB) return frameA - frameB;
  return String(a?.filename || "").localeCompare(String(b?.filename || ""));
}

export function jobKey(job) {
  return job?.group_id || job?.job_id || "";
}

export function buildAuthenticatedUrl(baseUrl, path, token, cacheBuster = null) {
  if (!baseUrl || !path) return "";
  const normalizedBase = String(baseUrl).trim().replace(/\/+$/, "");
  const url = new URL(path, `${normalizedBase}/`);
  if (token) url.searchParams.set("token", token);
  if (cacheBuster !== null && cacheBuster !== undefined) {
    url.searchParams.set("v", String(cacheBuster));
  }
  return url.toString();
}

export function summarizeDownloadActions(actions) {
  const parts = [];
  if (actions.downloaded) parts.push(i18n.t("downloads:summary.downloaded", { count: actions.downloaded }));
  if (actions.skipped) parts.push(i18n.t("downloads:summary.skipped", { count: actions.skipped }));
  if (actions.failed) parts.push(i18n.t("downloads:summary.failed", { count: actions.failed }));
  return parts.join(i18n.t("downloads:summarySeparator")) || i18n.t("myJobs:summarize.empty");
}

// Finds the task in a render group with the highest-index output frame
export function getLatestTaskWithOutput(tasks) {
  if (!Array.isArray(tasks)) return null;
  return tasks.reduce((best, task) => {
    if (!task?.latest_output_file) return best;
    if (!best?.latest_output_file) return task;
    const bestFrame = frameIndexFromFilename(best.latest_output_file);
    const taskFrame = frameIndexFromFilename(task.latest_output_file);
    if (taskFrame > bestFrame) return task;
    if (taskFrame === bestFrame && task.latest_output_file > best.latest_output_file) return task;
    return best;
  }, null);
}

// Constructs a preview URL for a render group's latest frame
export function getGroupPreviewUrl(job, backendUrl, authToken) {
  // Fast path: terminal groups have these snapshotted at the top level by
  // the backend, so we don't need to iterate `tasks` (which the list endpoint
  // returns empty for terminal groups).
  if (job?.latest_output_file && job?.latest_output_job_id) {
    const availableCount =
      typeof job.available_output_files_count === "number"
        ? job.available_output_files_count
        : 0;
    return buildAuthenticatedUrl(
      backendUrl,
      `/jobs/${job.latest_output_job_id}/output/${encodeURIComponent(job.latest_output_file)}/preview`,
      authToken,
      availableCount
    );
  }
  // Slow path: active groups still ship a `tasks` array — find the chunk
  // with the latest output and build the URL from it.
  const latestTask = getLatestTaskWithOutput(job?.tasks);
  if (!latestTask?.latest_output_file) return "";
  const availableCount =
    typeof job?.available_output_files_count === "number"
      ? job.available_output_files_count
      : (job?.tasks || []).reduce((sum, t) => sum + (t.output_files_count || 0), 0);
  return buildAuthenticatedUrl(
    backendUrl,
    `/jobs/${latestTask.job_id}/output/${encodeURIComponent(latestTask.latest_output_file)}/preview`,
    authToken,
    availableCount
  );
}

// Constructs a preview URL for a single job's latest frame
export function getSingleJobPreviewUrl(job, backendUrl, authToken) {
  const outputFiles = Array.isArray(job?.output_files) ? job.output_files : [];
  const latestFile =
    job?.latest_output_file ||
    outputFiles.slice().sort((a, b) => frameIndexFromFilename(a) - frameIndexFromFilename(b)).pop() ||
    "";
  if (!latestFile || !backendUrl) return "";
  const availableCount =
    typeof job?.output_files_count === "number" ? job.output_files_count : outputFiles.length;
  return buildAuthenticatedUrl(
    backendUrl,
    `/jobs/${job.job_id}/output/${encodeURIComponent(latestFile)}/preview`,
    authToken,
    availableCount
  );
}

// ---------------------------------------------------------------------------
// Job-table field accessors + labels (Phase 1 list view).
// All read ONLY fields already present on the /render-groups list DTO --
// no extra fetch.  Accessors return sortable primitives; *Label() return
// display strings.  Kept here (job-data helpers) so jobTableColumns stays
// purely declarative and the row/table components stay field-agnostic.
// ---------------------------------------------------------------------------

// Sort rank for status (active first, then terminal) -- a stable numeric so
// the status column sorts sensibly instead of alphabetically.
const STATUS_RANK = { running: 0, uploading: 1, pending: 2, done: 3, cancelled: 4, failed: 5 };
export function statusRank(status) {
  return STATUS_RANK[status] ?? 99;
}

// Number of GPUs (chunk instances) actively working a render.  Only in-flight
// groups carry `tasks`, so terminal/queued rows have no live GPU count and
// return 0 (rendered as "—").
export function jobGpuCount(job) {
  if (!Array.isArray(job?.tasks)) return 0;
  return job.tasks.filter((t) => t?.machine_gpu).length;
}

function renderSettings(job) {
  return job?.resolved_render_settings?.render || {};
}

export function jobTotalFrames(job) {
  return Number(job?.total_frames) || 0;
}

export function jobRenderedFrames(job) {
  return Number(job?.overall_rendered_frames) || 0;
}

export function jobProgressPct(job) {
  const pct = Number(job?.overall_progress_pct);
  return Number.isFinite(pct) ? pct : 0;
}

export function jobPixels(job) {
  const r = renderSettings(job);
  const x = Number(r.resolution_x) || 0;
  const y = Number(r.resolution_y) || 0;
  const pct = Number(r.resolution_percentage) || 100;
  return Math.round(x * y * (pct / 100));
}

export function jobResolutionLabel(job) {
  const r = renderSettings(job);
  if (!r.resolution_x || !r.resolution_y) return "—";
  const pct = Number(r.resolution_percentage);
  const base = `${r.resolution_x}×${r.resolution_y}`;
  return pct && pct !== 100 ? `${base} @${pct}%` : base;
}

export function jobSamples(job) {
  const s = Number(renderSettings(job).cycles_samples);
  return Number.isFinite(s) ? s : null;
}

export function jobEngineLabel(job) {
  const e = renderSettings(job).engine;
  if (!e) return "—";
  if (/EEVEE/i.test(e)) return "EEVEE";
  if (/CYCLES/i.test(e)) return "Cycles";
  return e;
}

export function jobOutputFormat(job) {
  return job?.resolved_render_settings?.output?.file_format || "";
}

export function jobOutputLabel(job) {
  const f = jobOutputFormat(job);
  if (!f) return "—";
  if (/EXR/i.test(f)) return /MULTILAYER/i.test(f) ? i18n.t("myJobs:output.exrMultilayer") : "EXR";
  return f;
}

export function jobFileSizeBytes(job) {
  return Number(job?.heaviness?.file_size_bytes) || 0;
}

// Total scene geometry — a "heaviness" proxy distinct from raw .blend size
// (a light file can still be geometry-heavy, and vice-versa).
export function jobVertexCount(job) {
  return Number(job?.heaviness?.vertex_count_total) || 0;
}

// Compact big-number label: 12_300_000_000 -> "12.3B".  Shared by the Stats
// page (pixels pushed, vertex counts) so the abbreviation reads the same
// everywhere.
export function formatCompactNumber(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return "—";
  const units = [["T", 1e12], ["B", 1e9], ["M", 1e6], ["K", 1e3]];
  for (const [suffix, div] of units) {
    if (v >= div) return `${(v / div).toFixed(1)}${suffix}`;
  }
  return String(Math.round(v));
}

export function jobSubmittedMs(job) {
  const t = Date.parse(job?.submitted_at || "");
  return Number.isFinite(t) ? t : 0;
}

// Wall time: submitted -> completed (or now, for in-flight rows).
export function jobDurationSec(job) {
  const start = Date.parse(job?.submitted_at || "");
  if (!Number.isFinite(start)) return 0;
  const end = job?.completed_at ? Date.parse(job.completed_at) : Date.now();
  if (!Number.isFinite(end)) return 0;
  return Math.max(0, Math.floor((end - start) / 1000));
}

export function formatBytesLabel(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i > 0 && v < 10 ? 1 : 0)} ${units[i]}`;
}

export function formatDurationLabel(totalSec) {
  const s = Number(totalSec);
  if (!Number.isFinite(s) || s <= 0) return "—";
  if (s < 60) return `${s}s`;
  const mins = Math.floor(s / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hrs < 24) return remMins > 0 ? `${hrs}h ${remMins}m` : `${hrs}h`;
  const days = Math.floor(hrs / 24);
  const remHrs = hrs % 24;
  return remHrs > 0 ? `${days}d ${remHrs}h` : `${days}d`;
}

export function formatDateTimeLabel(iso) {
  const t = Date.parse(iso || "");
  if (!Number.isFinite(t)) return "—";
  const d = new Date(t);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

// The REAL (actual) cost in credits, or 0 when there isn't one.  NO estimate
// fallback -- the Cost column shows the real cost or nothing.  Active renders
// surface their accrued-so-far actual; pending / legacy / never-charged rows
// are 0 and render as "—".
export function jobCostCredits(job) {
  const actual = Number(job?.total_actual_cost_credits);
  return Number.isFinite(actual) && actual > 0 ? actual : 0;
}

// Cost-column ordering.  Three tiers, because cost isn't a flat number:
//   0  in-progress renders                            -> pinned TOP
//   1  finished jobs with a real actual cost          -> middle, by cost
//   2  finished jobs with NO actual cost (none/legacy) -> pinned BOTTOM
// The top/bottom pins are direction-independent (tierA - tierB is never
// flipped) -- only the middle tier reverses on asc/desc -- so "in progress"
// stays up top and "no cost" stays at the bottom however the user toggles.
function costTier(job) {
  if (!isTerminalStatus(job?.status)) return 0;       // ongoing -> top
  return jobCostCredits(job) > 0 ? 1 : 2;             // real cost / none -> bottom
}

export function compareJobCost(a, b, dir) {
  const tierA = costTier(a);
  const tierB = costTier(b);
  if (tierA !== tierB) return tierA - tierB;           // fixed tier order
  if (tierA === 2) return 0;                           // no-cost tier: stable
  // Same orderable tier: sort by the real cost in the chosen direction.
  const ca = jobCostCredits(a);
  const cb = jobCostCredits(b);
  const cmp = ca === cb ? 0 : ca < cb ? -1 : 1;
  return dir === "desc" ? -cmp : cmp;
}
