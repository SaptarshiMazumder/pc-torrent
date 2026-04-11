import { createContext, useCallback, useContext, useRef, useState } from "react";
import { downloadJobOutputToDownloads } from "../services/sidecar";
import { buildDownloadFolderName } from "../utils/jobUtils";

const DownloadContext = createContext(null);

export function DownloadProvider({ children }) {
  const [downloads, setDownloads] = useState({});
  const activeRef = useRef(new Set());

  const startDownload = useCallback(async (id, jobName, fetchOutputs) => {
    if (activeRef.current.has(id)) return;
    activeRef.current.add(id);

    setDownloads((prev) => ({
      ...prev,
      [id]: {
        id,
        jobName,
        status: "loading",
        totalFiles: 0,
        completedFiles: 0,
        downloaded: 0,
        skipped: 0,
        failed: 0,
        error: "",
        path: "",
        startedAt: Date.now(),
        completedAt: null,
      },
    }));

    try {
      const jobFolder = buildDownloadFolderName(jobName, id);
      const data = await fetchOutputs();
      const files = Array.isArray(data?.files) ? data.files : [];
      if (files.length === 0) throw new Error("No output files found");

      setDownloads((prev) => ({
        ...prev,
        [id]: { ...prev[id], totalFiles: files.length },
      }));

      let lastPath = "";
      const stats = { downloaded: 0, skipped: 0, failed: 0 };

      for (let i = 0; i < files.length; i++) {
        const { filename, url: fileUrl, size_bytes: sizeBytes } = files[i];
        try {
          const result = await downloadJobOutputToDownloads(fileUrl, {
            jobFolder,
            preferredFilename: filename,
            expectedSizeBytes: Number.isFinite(sizeBytes) ? sizeBytes : null,
            overwriteExisting: false,
          });
          const action = String(result?.action || "downloaded");
          if (action === "skipped") stats.skipped += 1;
          else stats.downloaded += 1;
          if (result?.path) lastPath = result.path.replace(/[^\\/]+$/, "");
        } catch {
          stats.failed += 1;
        }

        setDownloads((prev) => ({
          ...prev,
          [id]: {
            ...prev[id],
            completedFiles: i + 1,
            ...stats,
          },
        }));
      }

      setDownloads((prev) => ({
        ...prev,
        [id]: {
          ...prev[id],
          status: stats.failed === files.length ? "error" : "done",
          error: stats.failed === files.length ? "All files failed to download" : "",
          path: lastPath || "Downloads",
          completedAt: Date.now(),
        },
      }));
    } catch (error) {
      setDownloads((prev) => ({
        ...prev,
        [id]: {
          ...prev[id],
          status: "error",
          error: error.message || "Download failed",
          completedAt: Date.now(),
        },
      }));
    } finally {
      activeRef.current.delete(id);
    }
  }, []);

  const isDownloading = useCallback(
    (id) => activeRef.current.has(id),
    []
  );

  const removeDownload = useCallback((id) => {
    if (activeRef.current.has(id)) return;
    setDownloads((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const clearCompleted = useCallback(() => {
    setDownloads((prev) => {
      const next = {};
      for (const [k, v] of Object.entries(prev)) {
        if (v.status === "loading") next[k] = v;
      }
      return next;
    });
  }, []);

  return (
    <DownloadContext.Provider value={{ downloads, startDownload, isDownloading, removeDownload, clearCompleted }}>
      {children}
    </DownloadContext.Provider>
  );
}

export const useDownloads = () => useContext(DownloadContext);
