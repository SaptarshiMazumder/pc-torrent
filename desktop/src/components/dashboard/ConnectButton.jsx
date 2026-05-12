import { useCallback, useEffect, useRef, useState } from "react";
import { connectAgent, disconnectAgent } from "../../services/sidecar";
import { getFirebaseToken } from "../../services/api";
import Loader from "../common/Loader";

// Server-side floor on the commitment window is "must be > 0".  We
// enforce a friendlier 15-minute minimum in the UI so users don't
// accidentally pick a window that expires before the first job can
// even be dispatched.
const MIN_COMMITMENT_SECONDS = 15 * 60;

// Quick-pick presets shown as chips under the datetime input.  Each
// just prefills the input to ``now + N hours`` (rounded to the next
// 5min for tidiness).
const COMMITMENT_PRESETS_HOURS = [1, 4, 8, 24];

function pad2(n) {
  return n < 10 ? `0${n}` : String(n);
}

/** Format a Date as the ``YYYY-MM-DDTHH:MM`` string an <input type="datetime-local"> expects. */
function toLocalInputValue(date) {
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}` +
    `T${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  );
}

/** Round up to the next 5-minute mark.  Keeps preset values visually clean. */
function roundUpToFiveMinutes(date) {
  const rounded = new Date(date);
  const minutes = rounded.getMinutes();
  rounded.setMinutes(minutes + ((5 - (minutes % 5)) % 5), 0, 0);
  return rounded;
}

export default function ConnectButton({ status, backendUrl, runtimeInfo }) {
  const isConnected = ["connected", "rendering", "paused"].includes(status);
  const preflightRunning =
    status === "checking_requirements" ||
    status === "setting_up_docker";
  const connectRunning =
    status === "downloading_image" ||
    status === "installing_image" ||
    status === "registering";
  const removingImage = status === "removing_image";
  const isLoading =
    preflightRunning ||
    connectRunning ||
    removingImage;
  const preflightComplete = runtimeInfo?.preflight_complete === true;
  const disabled = isConnected ? connectRunning : isLoading || !preflightComplete;

  // Popover state -- only relevant on the "Connect" press, not "Disconnect".
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerValue, setPickerValue] = useState("");
  const [pickerError, setPickerError] = useState("");
  const popoverRef = useRef(null);

  // Click-outside-to-close.
  useEffect(() => {
    if (!pickerOpen) return undefined;
    const onDown = (e) => {
      if (popoverRef.current && !popoverRef.current.contains(e.target)) {
        setPickerOpen(false);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [pickerOpen]);

  // ``min`` attribute for the datetime input -- next 5min mark.
  const minAllowed = roundUpToFiveMinutes(
    new Date(Date.now() + MIN_COMMITMENT_SECONDS * 1000),
  );
  const minInputValue = toLocalInputValue(minAllowed);

  const applyPreset = useCallback((hours) => {
    const next = roundUpToFiveMinutes(new Date(Date.now() + hours * 3600 * 1000));
    setPickerValue(toLocalInputValue(next));
    setPickerError("");
  }, []);

  const handleConnectConfirm = useCallback(async () => {
    if (!pickerValue) {
      setPickerError("Pick a date and time.");
      return;
    }
    const chosen = new Date(pickerValue);
    if (Number.isNaN(chosen.getTime())) {
      setPickerError("Invalid date/time.");
      return;
    }
    const seconds = Math.floor((chosen.getTime() - Date.now()) / 1000);
    if (seconds < MIN_COMMITMENT_SECONDS) {
      setPickerError("Pick a time at least 15 minutes from now.");
      return;
    }
    try {
      const token = await getFirebaseToken();
      await connectAgent(backendUrl, token || "", seconds);
      setPickerOpen(false);
      setPickerValue("");
      setPickerError("");
    } catch (err) {
      console.error("Agent connect failed:", err);
      setPickerError(`Failed: ${err?.message || err}`);
    }
  }, [pickerValue, backendUrl]);

  const handleButtonClick = useCallback(async () => {
    if (isConnected) {
      try {
        await disconnectAgent();
      } catch (err) {
        console.error("Agent disconnect failed:", err);
        alert(`Failed: ${err}`);
      }
      return;
    }
    setPickerError("");
    setPickerOpen(true);
  }, [isConnected]);

  return (
    <div className="connect-btn-wrap">
      <button
        className={`connect-btn ${isConnected ? "connected" : ""} ${isLoading ? "loading" : ""}`}
        onClick={handleButtonClick}
        disabled={disabled}
      >
        {preflightRunning ? (
          <>
            <Loader size="sm" />
            Checking...
          </>
        ) : connectRunning ? (
          <>
            <Loader size="sm" />
            Connecting...
          </>
        ) : isConnected ? (
          "Disconnect"
        ) : (
          "Connect"
        )}
      </button>

      {pickerOpen && (
        <div className="connect-commitment-popover" ref={popoverRef} role="dialog" aria-label="Choose availability window">
          <div className="connect-commitment-head">
            <span>How long will your PC be available?</span>
          </div>

          <div className="connect-commitment-presets">
            {COMMITMENT_PRESETS_HOURS.map((h) => (
              <button
                key={h}
                type="button"
                className="connect-commitment-preset"
                onClick={() => applyPreset(h)}
              >
                {h}h
              </button>
            ))}
          </div>

          <label className="connect-commitment-label">
            Available until
            <input
              type="datetime-local"
              className="connect-commitment-input"
              value={pickerValue}
              min={minInputValue}
              onChange={(e) => {
                setPickerValue(e.target.value);
                setPickerError("");
              }}
            />
          </label>

          {pickerError && (
            <div className="connect-commitment-error">{pickerError}</div>
          )}

          <div className="connect-commitment-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setPickerOpen(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={handleConnectConfirm}
              disabled={!pickerValue}
            >
              Connect
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
