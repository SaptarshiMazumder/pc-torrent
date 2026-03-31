import { connectAgent, disconnectAgent } from "../lib/sidecar";

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
  const blockedByReboot = status === "needs_reboot";
  const disabled = isConnected ? connectRunning : isLoading || blockedByReboot;

  const handleClick = async () => {
    try {
      if (isConnected) {
        await disconnectAgent();
      } else {
        await connectAgent(backendUrl);
      }
    } catch (err) {
      console.error("Agent command failed:", err);
      alert("Failed: " + err);
    }
  };

  return (
    <button
      className={`connect-btn ${isConnected ? "connected" : ""} ${isLoading ? "loading" : ""}`}
      onClick={handleClick}
      disabled={disabled}
    >
      {preflightRunning ? (
        <>
          <span className="spinner" />
          Checking...
        </>
      ) : connectRunning ? (
        <>
          <span className="spinner" />
          Connecting...
        </>
      ) : isConnected ? (
        "Disconnect"
      ) : (
        "Connect"
      )}
    </button>
  );
}
