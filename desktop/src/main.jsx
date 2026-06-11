import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { AuthProvider } from "./contexts/AuthContext";
import { DownloadProvider } from "./contexts/DownloadContext";
import { ErrorProvider } from "./contexts/ErrorContext";
import { ToastProvider } from "./contexts/ToastContext";
import { UserProfileProvider } from "./contexts/UserProfileContext";
import "./App.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <AuthProvider>
      <UserProfileProvider>
        <ToastProvider>
          <DownloadProvider>
            <ErrorProvider>
              <App />
            </ErrorProvider>
          </DownloadProvider>
        </ToastProvider>
      </UserProfileProvider>
    </AuthProvider>
  </React.StrictMode>
);
