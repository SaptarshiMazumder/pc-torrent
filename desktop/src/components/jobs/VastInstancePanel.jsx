import { useMemo } from "react";
import { getVastInstances } from "../../services/api";
import { formatCredits } from "../../utils/creditsFormat";
import InstancePanel from "./InstancePanel";

const VAST_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round">
    <rect x="2" y="6" width="20" height="12" rx="2" />
    <path d="M6 14h.01M10 14h.01M14 14h.01" />
  </svg>
);

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").replace(/^Vast\s+/i, "").trim();
}

const vastProvider = {
  title: "Vast Instances",
  icon: VAST_ICON,

  filterTask(task) {
    const mt = task.machine_type;
    return mt === "vast_serverless" || (!mt && (task.machine_gpu || "").toLowerCase().includes("vast"));
  },

  fetchInstances(backendUrl) {
    return getVastInstances(backendUrl);
  },

  extractCardData(task, live) {
    const liveVram = live?.gpu_vram_gb ? `${Math.round(live.gpu_vram_gb)}GB` : "";
    const gpuLabel = live?.gpu_model ? `${live.gpu_model} ${liveVram}`.trim() : gpuShortName(task.machine_gpu);
    const actualStatus = live?.actual_status ?? null;
    const displayStatus = actualStatus || task.status;
    const rendered = task.rendered_frames ?? 0;
    const total = task.total_frames;
    const rangeLabel = task.frame_start != null && task.frame_end != null ? `${task.frame_start}–${task.frame_end}` : null;
    return {
      gpuLabel,
      displayStatus,
      rendered,
      total,
      rangeLabel,
      elapsedSec: live?.elapsed_sec ?? null,
      cost: task.estimated_cost_credits != null
        ? `~${formatCredits(Number(task.estimated_cost_credits))} credits est.`
        : null,
      error: live?.error || (task.status === "failed" ? task.error : "") || "",
      stallRule: task.stall_rule || null,
      statusMsg: live?.status_msg || "",
      logs: live?.logs || "",
      history: live?.status_history || [],
    };
  },
};

export default function VastInstancePanel({ tasks, backendUrl, onRefresh, mode }) {
  const provider = useMemo(() => vastProvider, []);
  return <InstancePanel tasks={tasks} backendUrl={backendUrl} provider={provider} onRefresh={onRefresh} mode={mode} />;
}
