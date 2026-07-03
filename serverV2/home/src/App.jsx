import { useEffect, useState } from "react";
import { api, setTokenProvider } from "./api.js";
import { doSignOut, googleSignIn, initAuth, watchAuth } from "./firebaseClient.js";
import AdminDashboard from "./admin/AdminDashboard.jsx";
import UserDashboard from "./user/UserDashboard.jsx";

// phases: booting -> unconfigured | signedout | loading-profile | ready
export default function App() {
  const [phase, setPhase] = useState("booting");
  const [auth, setAuth] = useState(null);
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let unsub = null;
    initAuth()
      .then((instance) => {
        if (!instance) {
          setPhase("unconfigured");
          return;
        }
        setAuth(instance);
        setTokenProvider(() => instance.currentUser?.getIdToken() ?? null);
        unsub = watchAuth(instance, async (user) => {
          if (!user) {
            setProfile(null);
            setPhase("signedout");
            return;
          }
          setPhase("loading-profile");
          try {
            setProfile(await api("/me"));
            setPhase("ready");
          } catch (e) {
            setError(e);
            setPhase("signedout");
          }
        });
      })
      .catch((e) => {
        setError(e);
        setPhase("unconfigured");
      });
    return () => unsub?.();
  }, []);

  if (phase === "booting" || phase === "loading-profile") {
    return <div className="loading">Loading…</div>;
  }

  if (phase === "unconfigured") {
    return (
      <div className="login">
        <h1>Forge <span>Dashboard</span></h1>
        <div className="notice">
          Sign-in isn't configured on this server yet. Set the
          {" "}<code>FIREBASE_WEB_CONFIG_JSON</code> environment variable to this
          env's Firebase <em>web</em> SDK config (one-line JSON) and redeploy.
          {error ? <div style={{ marginTop: 8 }}>({String(error.message || error)})</div> : null}
        </div>
      </div>
    );
  }

  if (phase === "signedout") {
    return (
      <div className="login">
        <h1>Forge <span>Dashboard</span></h1>
        <p>Sign in with the Google account you use for Forge. Admins see the
          fleet console; everyone else sees their own renders.</p>
        {error ? <div className="error-banner">{String(error.message || error)}</div> : null}
        <button
          className="btn"
          onClick={() => googleSignIn(auth).catch((e) => setError(e))}
        >
          Sign in with Google
        </button>
      </div>
    );
  }

  const isAdmin = profile?.role === "admin";
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">Forge <span>Dashboard</span></div>
        {isAdmin ? <span className="badge admin">admin</span> : <span className="badge">user</span>}
        <span className="spacer" />
        <span className="who">{profile?.email}</span>
        <button className="btn ghost small" onClick={() => doSignOut(auth)}>Sign out</button>
      </header>
      {isAdmin ? <AdminDashboard /> : <UserDashboard profile={profile} />}
    </div>
  );
}
