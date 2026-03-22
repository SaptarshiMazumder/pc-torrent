import { useState, useEffect, useCallback, useRef } from "react";
import { listen } from "@tauri-apps/api/event";
import { getAgentState } from "../lib/sidecar";

const INITIAL_STATE = {
  status: "disconnected",
  message: "Ready",
  machineId: "",
  systemInfo: null,
  currentJob: null,
  logs: [],
};

export function useAgent() {
  const [state, setState] = useState(INITIAL_STATE);
  const logsRef = useRef([]);

  const addLog = useCallback((entry) => {
    logsRef.current = [...logsRef.current.slice(-499), entry];
    setState((prev) => ({ ...prev, logs: logsRef.current }));
  }, []);

  useEffect(() => {
    // Load initial state
    getAgentState()
      .then((data) => {
        if (data) {
          logsRef.current = data.logs || [];
          setState({
            status: data.status || "disconnected",
            message: data.message || "Ready",
            machineId: data.machine_id || "",
            systemInfo: data.system_info || null,
            currentJob: data.current_job || null,
            logs: data.logs || [],
          });
        }
      })
      .catch(() => {});

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
                  }
                : data.state === "connected" || data.state === "disconnected"
                  ? null
                  : prev.currentJob,
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
            message: `Job ${data.job_id} ${data.status}: ${data.error || "completed"}`,
          });
          break;
      }
    });

    return () => {
      unlisten.then((fn) => fn());
    };
  }, [addLog]);

  return state;
}
