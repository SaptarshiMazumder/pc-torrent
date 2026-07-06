import { useState, useEffect, useCallback, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { clearLogs as clearLogsCommand, getAgentState, runPreflight } from "../services/sidecar";
import i18n from "../i18n/i18n";

const INITIAL_RUNTIME_INFO = {
  preflight_complete: false,
  preflight_passed: null,
  preflight_message: i18n.t("shared:agent.preflightNotRun"),
  requirements_checked: false,
  requirements_ready: null,
  requirement_issues: [],
  docker_installed: null,
  docker_running: null,
  gpu_verified: null,
  image_present: null,
  image_stage: "idle",
  image_downloaded_bytes: null,
  image_total_bytes: null,
  image_progress_pct: null,
  image_status: i18n.t("shared:agent.notCheckedYet"),
  awaiting_uac: false,
  uac_message: "",
};

const INITIAL_STATE = {
  status: "disconnected",
  message: i18n.t("shared:agent.ready"),
  machineId: "",
  systemInfo: null,
  runtimeInfo: INITIAL_RUNTIME_INFO,
  preflightSteps: [],
  currentJob: null,
  logs: [],
};

export function useAgent(backendUrl) {
  const [state, setState] = useState(INITIAL_STATE);
  const logsRef = useRef([]);

  const addLog = useCallback((entry) => {
    logsRef.current = [...logsRef.current.slice(-999), entry];
    setState((prev) => ({ ...prev, logs: logsRef.current }));
  }, []);

  useEffect(() => {
    // Skip sidecar entirely when in rentee mode (no backendUrl)
    if (!backendUrl) return;

    let unlistenFn = null;

    // Listen for sidecar events
    const unlisten = listen("agent-event", (event) => {
      const data = event.payload;
      if (!data || !data.event) return;

      switch (data.event) {
        case "status":
          setState((prev) => ({
            ...prev,
            status: data.state || prev.status,
            message: data.message || prev.message,
            machineId: data.machine_id || prev.machineId,
            currentJob:
              data.state === "rendering"
                ? {
                    job_id: data.job_id || "",
                    filename: data.filename || "",
                    status: "rendering",
                    current_frame: null,
                    rendered_frames: null,
                    total_frames: null,
                    progress_pct: null,
                  }
                : data.state === "connected" || data.state === "disconnected"
                  ? null
                  : prev.currentJob,
          }));
          break;

        case "job_progress":
          setState((prev) => ({
            ...prev,
            currentJob: {
              job_id: data.job_id || prev.currentJob?.job_id || "",
              filename: data.filename || prev.currentJob?.filename || "",
              status: "rendering",
              current_frame:
                typeof data.current_frame === "number"
                  ? data.current_frame
                  : prev.currentJob?.current_frame ?? null,
              rendered_frames:
                typeof data.rendered_frames === "number"
                  ? data.rendered_frames
                  : prev.currentJob?.rendered_frames ?? null,
              total_frames:
                typeof data.total_frames === "number"
                  ? data.total_frames
                  : prev.currentJob?.total_frames ?? null,
              progress_pct:
                typeof data.progress_pct === "number"
                  ? data.progress_pct
                  : prev.currentJob?.progress_pct ?? null,
            },
          }));
          break;

        case "system_info":
          setState((prev) => ({
            ...prev,
            systemInfo: {
              gpu_name: data.gpu_name || "",
              gpu_vram_gb: data.gpu_vram_gb || 0,
              cpu_cores: data.cpu_cores || 0,
              ram_gb: data.ram_gb || 0,
              os_version: data.os_version || "",
              nvidia_driver: data.nvidia_driver || "",
              ready: data.ready || false,
              issues: data.issues || [],
            },
          }));
          break;

        case "runtime_info":
          setState((prev) => ({
            ...prev,
            runtimeInfo: {
              preflight_complete: data.preflight_complete || false,
              preflight_passed:
                typeof data.preflight_passed === "boolean"
                  ? data.preflight_passed
                  : null,
              preflight_message: data.preflight_message || i18n.t("shared:agent.preflightNotRun"),
              requirements_checked: data.requirements_checked || false,
              requirements_ready:
                typeof data.requirements_ready === "boolean"
                  ? data.requirements_ready
                  : null,
              requirement_issues: data.requirement_issues || [],
              docker_installed:
                typeof data.docker_installed === "boolean"
                  ? data.docker_installed
                  : null,
              docker_running:
                typeof data.docker_running === "boolean"
                  ? data.docker_running
                  : null,
              gpu_verified:
                typeof data.gpu_verified === "boolean"
                  ? data.gpu_verified
                  : null,
              image_present:
                typeof data.image_present === "boolean"
                  ? data.image_present
                  : null,
              image_stage: data.image_stage || "idle",
              image_downloaded_bytes:
                typeof data.image_downloaded_bytes === "number"
                  ? data.image_downloaded_bytes
                  : null,
              image_total_bytes:
                typeof data.image_total_bytes === "number"
                  ? data.image_total_bytes
                  : null,
              image_progress_pct:
                typeof data.image_progress_pct === "number"
                  ? data.image_progress_pct
                  : null,
              image_status: data.image_status || "",
              // UAC state resets to false when runtime_info arrives (install finished)
              awaiting_uac: false,
              uac_message: "",
            },
          }));
          break;

        case "uac_prompt":
          setState((prev) => ({
            ...prev,
            runtimeInfo: {
              ...prev.runtimeInfo,
              awaiting_uac: true,
              uac_message: data.message || i18n.t("shared:agent.uacFallback"),
            },
          }));
          break;

        case "preflight_steps":
          setState((prev) => ({
            ...prev,
            preflightSteps: data.steps || [],
          }));
          break;

        case "log":
          addLog({
            level: data.level || "info",
            source: data.source || "agent",
            message: data.message || "",
          });
          break;

        case "error":
          addLog({
            level: "error",
            source: "agent",
            message: data.message || "",
          });
          break;

        case "job_complete":
          setState((prev) => ({
            ...prev,
            currentJob: null,
          }));
          addLog({
            level: "info",
            source: "agent",
            message: i18n.t("shared:agent.jobLog", {
              jobId: data.job_id,
              status: data.status,
              detail: data.error || i18n.t("shared:agent.jobCompleted"),
            }),
          });
          break;
      }
    }).then((fn) => {
      unlistenFn = fn;

      getAgentState()
        .then((data) => {
          if (data) {
            logsRef.current = data.logs || [];
            setState({
              status: data.status || "disconnected",
              message: data.message || i18n.t("shared:agent.ready"),
              machineId: data.machine_id || "",
              systemInfo: data.system_info || null,
              runtimeInfo: data.runtime_info || INITIAL_RUNTIME_INFO,
              preflightSteps: data.preflight_steps || [],
              currentJob: data.current_job || null,
              logs: data.logs || [],
            });
          }

          runPreflight().catch(() => {});
        })
        .catch(() => {
          runPreflight().catch(() => {});
        });

      return fn;
    });

    return () => {
      if (unlistenFn) {
        unlistenFn();
      } else {
        unlisten.then((fn) => fn());
      }
    };
  }, [addLog, backendUrl]);

  const clearLogs = useCallback(async () => {
    await clearLogsCommand();
    logsRef.current = [];
    setState((prev) => ({ ...prev, logs: [] }));
  }, []);

  return {
    ...state,
    clearLogs,
  };
}
