// ---------------------------------------------------------------------------
// Telemetry derivation — pure functions over the render-group DTOs the app
// already holds (ongoing + past).  No network, no React.  Everything here is
// computed client-side from fields present on the /render-groups list DTO, so
// the Stats page needs zero new backend surface.  Reuses the job-field
// accessors from jobUtils so a field's meaning is defined in exactly one place.
//
// Note on GPUs: past (terminal) groups ship no `tasks`, so per-GPU time/cost
// is derivable only for IN-FLIGHT renders (active groups still carry tasks).
// aggregateLive() owns that; the lifetime rollup deliberately stays GPU-free.
// ---------------------------------------------------------------------------

import {
  isTerminalStatus,
  jobTotalFrames,
  jobRenderedFrames,
  jobFileSizeBytes,
  jobVertexCount,
  jobEngineLabel,
  jobResolutionLabel,
  jobPixels,
  jobDurationSec,
  jobCostCredits,
  resolveJobFilename,
  jobKey,
} from "./jobUtils";

const FPS = 24;

function statusOf(job) {
  return job?.status || "pending";
}

// Frames a render actually delivered.  A finished ("done") group delivered
// its full range; anything else (failed / cancelled / in-flight) delivered
// only what it got through, which the DTO tracks as overall_rendered_frames.
function deliveredFrames(job) {
  return statusOf(job) === "done" ? jobTotalFrames(job) : jobRenderedFrames(job);
}

// Strip vendor prefixes so "Vast RTX 4080 SUPER 16GB" -> "RTX 4080 SUPER 16GB".
// Mirrors the label the instance panels show, so live GPU rows read the same
// as they do on the job-detail page.
function gpuShortName(name) {
  if (!name) return "GPU";
  return (
    String(name)
      .replace(/^Vast\s+/i, "")
      .replace(/^Modal\s+/i, "")
      .replace(/nvidia\s+/i, "")
      .replace(/geforce\s+/i, "")
      .trim() || "GPU"
  );
}

// Count occurrences of keyFn(item) -> [{ name, value }] desc.
function splitBy(items, keyFn) {
  const counts = new Map();
  for (const item of items) {
    const key = keyFn(item);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  return [...counts.entries()]
    .map(([name, value]) => ({ name, value }))
    .sort((a, b) => b.value - a.value);
}

function maxBy(items, valueFn) {
  let best = null;
  let bestVal = -Infinity;
  for (const item of items) {
    const v = valueFn(item);
    if (v > bestVal) {
      bestVal = v;
      best = item;
    }
  }
  return best;
}

function modeOf(values) {
  if (!values.length) return null;
  const counts = new Map();
  let best = values[0];
  let bestCount = 0;
  for (const v of values) {
    const c = (counts.get(v) || 0) + 1;
    counts.set(v, c);
    if (c > bestCount) {
      bestCount = c;
      best = v;
    }
  }
  return best;
}

// Renders / frames / credits bucketed by calendar day, oldest -> newest.  Only
// days with activity appear, keeping a sparse history from stretching into a
// wall of empty columns.  Carries all three series so the token chart and any
// activity view read from one pass.
function bucketByDay(jobs) {
  const buckets = new Map();
  for (const job of jobs) {
    const t = Date.parse(job?.submitted_at || "");
    if (!Number.isFinite(t)) continue;
    const d = new Date(t);
    const dayTs = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const bucket = buckets.get(dayTs) || { ts: dayTs, count: 0, frames: 0, credits: 0 };
    bucket.count += 1;
    bucket.frames += deliveredFrames(job);
    bucket.credits += jobCostCredits(job);
    buckets.set(dayTs, bucket);
  }
  return [...buckets.values()]
    .sort((a, b) => a.ts - b.ts)
    .map((b) => ({
      ...b,
      credits: Math.round(b.credits * 10) / 10,
      label: new Date(b.ts).toLocaleDateString([], { month: "short", day: "numeric" }),
    }));
}

// One row per render with every sortable metric pre-extracted.  The leaderboard
// component picks which metric to rank + scale by; the data layer just supplies
// the numbers so the meaning of each field stays defined once (in jobUtils).
function buildRenderRows(jobs) {
  return jobs
    .map((job) => ({
      id: jobKey(job),
      name: resolveJobFilename(job),
      frames: jobTotalFrames(job),
      seconds: jobDurationSec(job),
      credits: jobCostCredits(job),
      bytes: jobFileSizeBytes(job),
      verts: jobVertexCount(job),
    }))
    .filter((r) => r.frames > 0 || r.bytes > 0);
}

function funFacts(jobs, done) {
  const framesRendered = jobs.reduce((sum, j) => sum + deliveredFrames(j), 0);
  const biggest = maxBy(jobs, jobTotalFrames);
  const heaviest = maxBy(jobs, jobFileSizeBytes);
  const avgDurationSec = done.length
    ? Math.round(done.reduce((sum, j) => sum + jobDurationSec(j), 0) / done.length)
    : 0;
  const favResolution = modeOf(
    jobs.map(jobResolutionLabel).filter((r) => r && r !== "—"),
  );
  return {
    footageSec: framesRendered / FPS,
    biggestFrames: biggest ? jobTotalFrames(biggest) : 0,
    biggestName: biggest ? resolveJobFilename(biggest) : "",
    heaviestBytes: heaviest ? jobFileSizeBytes(heaviest) : 0,
    heaviestName: heaviest ? resolveJobFilename(heaviest) : "",
    avgDurationSec,
    favResolution: favResolution || "—",
  };
}

// Lifetime rollup over every render the client holds (ongoing + past).
// Totals cover whatever history is loaded; the Stats page pulls the full
// terminal history first so these reflect everything, not just page one.
export function aggregateLifetime(jobs) {
  const done = jobs.filter((j) => statusOf(j) === "done");
  const engineSplit = splitBy(jobs, jobEngineLabel).filter((s) => s.name !== "—");

  return {
    totalRenders: jobs.length,
    completedRenders: done.length,
    framesRendered: jobs.reduce((sum, j) => sum + deliveredFrames(j), 0),
    outputFiles: jobs.reduce(
      (sum, j) => sum + (Number(j?.available_output_files_count) || 0),
      0,
    ),
    creditsSpent: jobs.reduce((sum, j) => sum + jobCostCredits(j), 0),
    sceneBytes: jobs.reduce((sum, j) => sum + jobFileSizeBytes(j), 0),
    pixelsPushed: jobs.reduce((sum, j) => sum + jobPixels(j) * deliveredFrames(j), 0),
    engineSplit,
    overTime: bucketByDay(jobs),
    renderRows: buildRenderRows(jobs),
    funFacts: funFacts(jobs, done),
  };
}

// Live rollup over in-flight renders only.  Active groups still carry their
// per-chunk `tasks`, so GPUs-in-use with time + cost come straight from the
// mirrored DTO — no extra reads.  Ranked by time so the "most used" GPU leads.
export function aggregateLive(ongoingJobs) {
  const tasks = ongoingJobs.flatMap((j) => (Array.isArray(j?.tasks) ? j.tasks : []));

  const gpuAgg = new Map();
  for (const t of tasks) {
    const name = gpuShortName(t?.machine_gpu);
    const cur = gpuAgg.get(name) || { name, chunks: 0, seconds: 0, credits: 0 };
    cur.chunks += 1;
    cur.seconds += Number(t?.actual_seconds) || 0;
    cur.credits += Number(t?.actual_cost_credits) || 0;
    gpuAgg.set(name, cur);
  }

  return {
    activeRenders: ongoingJobs.length,
    activeChunks: tasks.length,
    gpuUsage: [...gpuAgg.values()].sort((a, b) => b.seconds - a.seconds),
    framesInFlight: tasks.reduce((sum, t) => sum + (Number(t?.rendered_frames) || 0), 0),
    totalFramesInFlight: tasks.reduce((sum, t) => sum + (Number(t?.total_frames) || 0), 0),
    liveCredits: ongoingJobs.reduce((sum, j) => sum + jobCostCredits(j), 0),
  };
}
