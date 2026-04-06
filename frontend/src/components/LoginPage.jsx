import { useState } from "react";
import { useAuth } from "../contexts/AuthContext";

export default function LoginPage() {
  const { signIn, signUp, resetPassword } = useAuth();
  const [isSignUp, setIsSignUp] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setNotice("");
    setLoading(true);
    try {
      if (isSignUp) {
        await signUp(email, password);
      } else {
        await signIn(email, password);
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
      setError("Enter your email first, then click Forgot password.");
      return;
    }
    setError("");
    setNotice("");
    setLoading(true);
    try {
      await resetPassword(trimmedEmail);
      setNotice("Password reset link sent. Check your inbox and spam folder.");
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
        return "Invalid email or password.";
      case "auth/email-already-in-use":
        return "An account with this email already exists.";
      case "auth/weak-password":
        return "Password must be at least 6 characters.";
      case "auth/invalid-email":
        return "Please enter a valid email address.";
      case "auth/too-many-requests":
        return "Too many attempts. Please wait a bit and try again.";
      default:
        return "Something went wrong. Please try again.";
    }
  }

  return (
    <div className="login-container">
      <div className="login-card">
        <h1 className="login-title">PC Rent</h1>
        <p className="login-subtitle">Distributed GPU Rendering</p>

        <form onSubmit={handleSubmit} className="login-form">
          <h2>{isSignUp ? "Create account" : "Sign in"}</h2>

          <label>
            Email
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
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={isSignUp ? "Min. 6 characters" : "Password"}
              required
            />
          </label>

          {!isSignUp && (
            <div className="login-helper">
              <button
                type="button"
                className="btn-link"
                onClick={handlePasswordReset}
                disabled={loading}
              >
                Forgot password?
              </button>
            </div>
          )}

          {error && <p className="login-error">{error}</p>}
          {notice && <p className="login-success">{notice}</p>}

          <button type="submit" className="btn-primary" disabled={loading}>
            {loading ? "Please wait..." : isSignUp ? "Create account" : "Sign in"}
          </button>
        </form>

        <p className="login-toggle">
          {isSignUp ? "Already have an account?" : "Don't have an account?"}
          <button
            className="btn-link"
            type="button"
            onClick={() => {
              setIsSignUp((v) => !v);
              setError("");
              setNotice("");
            }}
          >
            {isSignUp ? "Sign in" : "Sign up"}
          </button>
        </p>
      </div>
    </div>
  );
}
