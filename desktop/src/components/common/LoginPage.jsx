import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useAuth } from "../../contexts/AuthContext";

export default function LoginPage() {
  const { t } = useTranslation("login");
  const { signIn, signUp, signInWithGoogle, cancelGoogleSignIn, resetPassword, rememberMe, setRememberMePreference } = useAuth();
  const [isSignUp, setIsSignUp] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);
  // Distinguishes a user-initiated cancel from a genuine failure so we don't
  // surface an error toast when the user simply backed out.
  const googleCancelledRef = useRef(false);

  async function handleGoogleSignIn() {
    // While a sign-in is pending, the button acts as a Cancel control.
    if (googleLoading) {
      googleCancelledRef.current = true;
      cancelGoogleSignIn();
      return;
    }
    setError("");
    setNotice("");
    googleCancelledRef.current = false;
    setGoogleLoading(true);
    try {
      await signInWithGoogle({ remember: rememberMe });
    } catch (err) {
      // Firebase errors carry a `code`; the native OAuth flow throws plain
      // Errors whose message is already user-readable.  Suppress the message
      // when the user cancelled on purpose.
      if (!googleCancelledRef.current) {
        setError(err?.code ? friendlyError(err.code) : (err?.message || t("errors.googleFailed")));
      }
    } finally {
      setGoogleLoading(false);
      googleCancelledRef.current = false;
    }
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setNotice("");
    setLoading(true);
    try {
      if (isSignUp) {
        await signUp(email, password, { remember: rememberMe });
      } else {
        await signIn(email, password, { remember: rememberMe });
      }
    } catch (err) {
      setError(friendlyError(err.code));
    } finally {
      setLoading(false);
    }
  }

  async function handlePasswordReset() {
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      setNotice("");
      setError(t("enterEmailFirst"));
      return;
    }
    setError("");
    setNotice("");
    setLoading(true);
    try {
      await resetPassword(trimmedEmail);
      setNotice(t("resetSent"));
    } catch (err) {
      setError(friendlyError(err.code));
    } finally {
      setLoading(false);
    }
  }

  function friendlyError(code) {
    switch (code) {
      case "auth/user-not-found":
      case "auth/wrong-password":
      case "auth/invalid-credential":
        return t("errors.invalidCredential");
      case "auth/email-already-in-use":
        return t("errors.emailInUse");
      case "auth/weak-password":
        return t("errors.weakPassword");
      case "auth/invalid-email":
        return t("errors.invalidEmail");
      case "auth/too-many-requests":
        return t("errors.tooManyRequests");
      case "auth/account-exists-with-different-credential":
        return t("errors.accountExistsDifferentCredential");
      default:
        return t("errors.generic");
    }
  }

  return (
    <div className="login-container">
      <div className="login-card">
        <h1 className="login-title">Forge</h1>
        <p className="login-subtitle">{t("subtitle")}</p>

        <form onSubmit={handleSubmit} className="login-form">
          <h2>{isSignUp ? t("createAccount") : t("signIn")}</h2>

          <label>
            {t("email")}
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              required
              autoFocus
            />
          </label>

          <label>
            {t("password")}
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isSignUp ? t("passwordPlaceholderSignUp") : t("passwordPlaceholderSignIn")}
              required
            />
          </label>

          {!isSignUp && (
            <label className="login-remember">
              <input
                type="checkbox"
                checked={!!rememberMe}
                onChange={(e) => setRememberMePreference(e.target.checked)}
                disabled={loading}
              />
              <span>{t("rememberMe")}</span>
            </label>
          )}

          {!isSignUp && (
            <div className="login-helper">
              <button
                type="button"
                className="btn-link"
                onClick={handlePasswordReset}
                disabled={loading}
              >
                {t("forgotPassword")}
              </button>
            </div>
          )}

          {error && <p className="login-error">{error}</p>}
          {notice && <p className="login-success">{notice}</p>}

          <button type="submit" className="btn btn-primary" disabled={loading || googleLoading}>
            {loading ? t("pleaseWait") : isSignUp ? t("createAccount") : t("signIn")}
          </button>
        </form>

        <div className="login-divider"><span>{t("or")}</span></div>

        <button
          type="button"
          className="btn btn-google"
          onClick={handleGoogleSignIn}
          disabled={loading}
        >
          {googleLoading ? (
            t("googleWaiting")
          ) : (
            <>
              <GoogleGlyph />
              {t("continueWithGoogle")}
            </>
          )}
        </button>

        <p className="login-toggle">
          {isSignUp ? t("haveAccount") : t("noAccount")}
          <button
            className="btn-link"
            type="button"
            onClick={() => {
              setIsSignUp((v) => !v);
              setError("");
              setNotice("");
            }}
          >
            {isSignUp ? t("signIn") : t("signUp")}
          </button>
        </p>
      </div>
    </div>
  );
}

function GoogleGlyph() {
  return (
    <svg viewBox="0 0 18 18" aria-hidden="true" focusable="false">
      <path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.71-1.57 2.68-3.89 2.68-6.62Z" />
      <path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.8.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z" />
      <path fill="#FBBC05" d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z" />
      <path fill="#EA4335" d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.47.9 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z" />
    </svg>
  );
}
