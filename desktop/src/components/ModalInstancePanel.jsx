import { useMemo } from "react";
import { getModalInstances } from "../lib/api";
import { formatCredits } from "../utils/creditsFormat";
import i18n from "../i18n/i18n";
import InstancePanel from "./jobs/InstancePanel";

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
  titleKey: "providers.modal",
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
      cost: task.estimated_cost_credits != null
        ? i18n.t("myJobs:cost.estTokens", { credits: formatCredits(Number(task.estimated_cost_credits)) })
        : null,
      error: live?.error || (task.status === "failed" ? task.error : "") || "",
      stallRule: task.stall_rule || null,
      statusMsg: live?.monitor_action || "",
      logs: live?.logs || "",
      history: Array.isArray(live?.status_history) ? live.status_history : [],
    };
  },
};

export default function ModalInstancePanel({ tasks, backendUrl, onRefresh, mode }) {
  const provider = useMemo(() => modalProvider, []);
  return <InstancePanel tasks={tasks} backendUrl={backendUrl} provider={provider} onRefresh={onRefresh} mode={mode} />;
}
