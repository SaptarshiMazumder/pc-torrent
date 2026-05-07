import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuthProvider } from "./contexts/AuthContext";
import { DownloadProvider } from "./contexts/DownloadContext";
import { ErrorProvider } from "./contexts/ErrorContext";
import "./App.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AuthProvider>
      <DownloadProvider>
        <ErrorProvider>
          <App />
        </ErrorProvider>
      </DownloadProvider>
    </AuthProvider>
  </React.StrictMode>
);
