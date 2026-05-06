import { useMemo } from "react";
import InstancePanel, { loadingStallChipParts } from "./InstancePanel";

const COMMUNITY_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M8 20h8M12 16v4" />
  </svg>
);

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").trim();
}

const communityProvider = {
  title: "Community",
  icon: COMMUNITY_ICON,

  filterTask(task) {
    return task.machine_type === "community";
  },

  // Community jobs have no separate live-polling endpoint — the agent
  // reports state directly via /jobs/{id}/progress and /heartbeat, which
  // populates the task object the parent already polls.  Returning null
  // tells InstancePanel's polling loop there's nothing to fetch.
  fetchInstances() {
    return Promise.resolve(null);
  },

  extractCardData(task, _live) {
    const gpuLabel = gpuShortName(task.machine_gpu);
    const rendered = task.rendered_frames ?? 0;
    const total = task.total_frames ?? null;
    const rangeLabel = task.frame_start != null && task.frame_end != null
      ? `${task.frame_start}–${task.frame_end}`
      : null;

    return {
      gpuLabel,
      displayStatus: task.status,
      rendered,
      total,
      rangeLabel,
      elapsedSec: null,
      cost: null,
      error: task.status === "failed" ? (task.error || "") : "",
      stallRule: task.stall_rule || null,
      loadingStall: loadingStallChipParts(task.estimated_startup_seconds),
      statusMsg: "",
      logs: "",
      history: [],
    };
  },
};

export default function CommunityInstancePanel({ tasks, backendUrl, onRefresh }) {
  const provider = useMemo(() => communityProvider, []);
  return <InstancePanel tasks={tasks} backendUrl={backendUrl} provider={provider} onRefresh={onRefresh} />;
}
