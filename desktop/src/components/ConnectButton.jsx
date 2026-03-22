import { connectAgent, disconnectAgent } from "../lib/sidecar";

export default function ConnectButton({ status, backendUrl }) {
  const isConnected =
    status !== "disconnected" && status !== "error" && status !== "needs_reboot";
  const isLoading =
    status === "checking_requirements" ||
    status === "setting_up_docker" ||
    status === "downloading_image" ||
    status === "registering";

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
      disabled={isLoading}
    >
      {isLoading ? (
        <>
          <span className="spinner" />
          Setting up...
        </>
      ) : isConnected ? (
        "Disconnect"
      ) : (
        "Connect"
      )}
    </button>
  );
}
