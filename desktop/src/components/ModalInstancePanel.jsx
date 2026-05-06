import { useMemo } from "react";
import { getModalInstances } from "../lib/api";
import InstancePanel, { loadingStallChipParts } from "./jobs/InstancePanel";

const MODAL_ICON = (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round">
    <path d="M12 2L2 7l10 5 10-5-10-5z" />
    <path d="M2 17l10 5 10-5" />
    <path d="M2 12l10 5 10-5" />
  </svg>
);

function gpuShortName(name) {
  if (!name) return "GPU";
  return name.replace(/nvidia\s+/i, "").replace(/geforce\s+/i, "").replace(/^Modal\s+/i, "").trim();
}

const modalProvider = {
  title: "Modal Instances",
  icon: MODAL_ICON,

  filterTask(task) {
    const mt = task.machine_type;
    return mt === "modal_serverless" || (!mt && (task.machine_gpu || "").toLowerCase().includes("modal"));
  },

  fetchInstances(backendUrl) {
    return getModalInstances(backendUrl);
  },

  extractCardData(task, live) {
    const gpuLabel = task.machine_gpu
      ? gpuShortName(task.machine_gpu)
      : live?.gpu_type
        ? `Modal ${String(live.gpu_type).toUpperCase()}`
        : "Modal GPU";

    const providerStatus = live?.provider_status ?? null;
    const displayStatus = providerStatus || task.status;
    const rendered = task.rendered_frames ?? 0;
    const total = live?.total_frames ?? task.total_frames ?? null;
    const rangeLabel = (live?.frame_start ?? task.frame_start) != null && (live?.frame_end ?? task.frame_end) != null
      ? `${live?.frame_start ?? task.frame_start}–${live?.frame_end ?? task.frame_end}`
      : null;

    return {
      gpuLabel,
      displayStatus,
      rendered,
      total,
      rangeLabel,
      elapsedSec: live?.elapsed_sec ?? null,
      cost: null,
      error: live?.error || (task.status === "failed" ? task.error : "") || "",
      stallRule: task.stall_rule || null,
      loadingStall: loadingStallChipParts(task.estimated_startup_seconds),
      statusMsg: live?.monitor_action || "",
      logs: live?.logs || "",
      history: Array.isArray(live?.status_history) ? live.status_history : [],
    };
  },
};

export default function ModalInstancePanel({ tasks, backendUrl, onRefresh }) {
  const provider = useMemo(() => modalProvider, []);
  return <InstancePanel tasks={tasks} backendUrl={backendUrl} provider={provider} onRefresh={onRefresh} />;
}
