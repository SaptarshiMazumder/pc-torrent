//! Loopback OAuth 2.0 authorization-code capture (RFC 8252 §7.3).
//!
//! Runs the redirect leg of an OAuth `code` + PKCE flow entirely on the
//! native side, so the credential round-trip never touches the webview:
//!
//!   1. Bind an ephemeral loopback listener (`127.0.0.1:0`).  Only this
//!      process learns the port, so no other local app can race the redirect.
//!   2. Open the fully-formed authorize URL (caller params + our
//!      `redirect_uri`) in the user's real system browser.
//!   3. Accept the single browser redirect, verify the `state` (CSRF), and
//!      extract the authorization `code`.
//!   4. Exchange `code` + PKCE `code_verifier` for tokens at the caller's
//!      token endpoint (native `reqwest`, which sidesteps webview CORS) and
//!      hand the raw token JSON back to the frontend.
//!
//! The module is provider-agnostic: every OAuth-specific value (endpoints,
//! scopes, client id/secret, PKCE, prompt) is supplied by the caller.  It
//! knows only "run a loopback code exchange with these params".

use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use reqwest::Url;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, State};
use tauri_plugin_shell::ShellExt;

/// Shared cancel signal for an in-flight loopback flow.  Managed by Tauri:
/// the capture loop polls it, `cancel_loopback_oauth` sets it, and each new
/// run clears it.  Lets the user abandon a browser sign-in and retry at once.
#[derive(Default)]
pub struct OAuthCancelFlag(Arc<AtomicBool>);

/// Served to the browser tab once the redirect is captured.
const RESPONSE_OK_HTML: &str = "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>Signed in</title></head><body style=\"font-family:system-ui;background:#0d0f14;\
color:#e8e8ea;display:flex;align-items:center;justify-content:center;height:100vh;\
margin:0\"><div style=\"text-align:center\"><h2>You're signed in to Forge</h2>\
<p>You can close this tab and return to the app.</p></div></body></html>";

const RESPONSE_ERR_HTML: &str = "<!doctype html><html><head><meta charset=\"utf-8\">\
<title>Sign-in failed</title></head><body style=\"font-family:system-ui;background:#0d0f14;\
color:#e8e8ea;display:flex;align-items:center;justify-content:center;height:100vh;\
margin:0\"><div style=\"text-align:center\"><h2>Sign-in was cancelled</h2>\
<p>You can close this tab and try again in the app.</p></div></body></html>";

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LoopbackOAuthRequest {
    authorize_endpoint: String,
    /// OAuth authorize params (client_id, response_type, scope, state,
    /// code_challenge, …).  `redirect_uri` is added by this module.
    authorize_params: HashMap<String, String>,
    token_endpoint: String,
    /// OAuth token params (client_id, client_secret, grant_type,
    /// code_verifier).  `code` and `redirect_uri` are added by this module.
    token_params: HashMap<String, String>,
    expected_state: String,
    timeout_secs: u64,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LoopbackOAuthResponse {
    /// Raw JSON from the token endpoint; the caller extracts `id_token`.
    token_response: serde_json::Value,
}

#[tauri::command]
#[allow(deprecated)] // Shell::open is deprecated for tauri-plugin-opener, but
                     // reusing the already-wired shell plugin avoids a new dep.
pub async fn run_loopback_oauth(
    app: AppHandle,
    cancel: State<'_, OAuthCancelFlag>,
    request: LoopbackOAuthRequest,
) -> Result<LoopbackOAuthResponse, String> {
    // Fresh start: clear any cancel left set by a previous attempt.
    cancel.0.store(false, Ordering::SeqCst);

    let listener = TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("Failed to bind loopback listener: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("Failed to read loopback port: {e}"))?
        .port();
    let redirect_uri = format!("http://127.0.0.1:{port}");

    // Build authorize URL from caller params + our redirect_uri, then let the
    // frontend open it in the user's real browser.
    let mut authorize_params: Vec<(&str, &str)> = request
        .authorize_params
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    authorize_params.push(("redirect_uri", redirect_uri.as_str()));
    let authorize_url = Url::parse_with_params(&request.authorize_endpoint, authorize_params)
        .map_err(|e| format!("Failed to build authorize URL: {e}"))?
        .to_string();
    // Open in the user's real system browser (RFC 8252 — never an embedded
    // webview).  `Shell::open` with no scope skips the JS-facing validation.
    app.shell()
        .open(&authorize_url, None)
        .map_err(|e| format!("Failed to open system browser: {e}"))?;

    // Capture the single redirect on a blocking thread, bounded by the
    // caller's timeout (covers the user closing or denying the browser).
    let expected_state = request.expected_state.clone();
    let timeout = Duration::from_secs(request.timeout_secs);
    let cancel_flag = cancel.0.clone();
    let code = tauri::async_runtime::spawn_blocking(move || {
        capture_redirect(listener, &expected_state, timeout, &cancel_flag)
    })
    .await
    .map_err(|e| format!("Redirect capture task panicked: {e}"))??;

    let token_response = exchange_code(
        &request.token_endpoint,
        &request.token_params,
        &code,
        &redirect_uri,
    )
    .await?;

    Ok(LoopbackOAuthResponse { token_response })
}

/// Cancel an in-flight `run_loopback_oauth` so the user can abandon a browser
/// sign-in and retry.  The capture loop notices within one poll and frees the port.
#[tauri::command]
pub fn cancel_loopback_oauth(cancel: State<'_, OAuthCancelFlag>) {
    cancel.0.store(true, Ordering::SeqCst);
}

/// Bounded read window per accepted connection, so a browser preconnect socket
/// that sends no bytes can't wedge the capture loop.
const CONNECTION_READ_TIMEOUT: Duration = Duration::from_millis(1500);

/// Accept connections until the OAuth redirect arrives (or the deadline lapses),
/// validate `state`, and return the authorization `code`.
///
/// Browsers (Chrome especially) open speculative preconnect sockets to the
/// loopback that never send a request; those are read-timed-out and skipped so
/// only the real redirect GET is acted on.
fn capture_redirect(
    listener: TcpListener,
    expected_state: &str,
    timeout: Duration,
    cancel: &AtomicBool,
) -> Result<String, String> {
    listener
        .set_nonblocking(true)
        .map_err(|e| format!("Failed to configure loopback listener: {e}"))?;
    let deadline = Instant::now() + timeout;
    loop {
        if cancel.load(Ordering::SeqCst) {
            return Err("Google sign-in cancelled.".to_string());
        }
        if Instant::now() >= deadline {
            return Err("Timed out waiting for Google sign-in.".to_string());
        }
        match listener.accept() {
            Ok((stream, _)) => {
                if let Some(code) = try_capture_code(stream, expected_state)? {
                    return Ok(code);
                }
                // Not the redirect (preconnect / favicon) — keep listening.
            }
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(100));
            }
            Err(e) => return Err(format!("Loopback accept failed: {e}")),
        }
    }
}

/// Inspect one accepted connection.
///
/// * `Ok(Some(code))` — this was the OAuth redirect and it's valid.
/// * `Ok(None)`       — not the redirect (empty preconnect, favicon, …); skip.
/// * `Err(_)`         — it was the redirect but failed (Google error / state mismatch).
fn try_capture_code(
    mut stream: TcpStream,
    expected_state: &str,
) -> Result<Option<String>, String> {
    // Bounded blocking read: a socket that sends nothing times out and is
    // skipped rather than blocking the loop forever.
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(CONNECTION_READ_TIMEOUT));

    let mut buf = [0u8; 8192];
    let n = match stream.read(&mut buf) {
        Ok(0) | Err(_) => return Ok(None),
        Ok(n) => n,
    };
    let head = String::from_utf8_lossy(&buf[..n]);
    let target = match head.lines().next().and_then(|l| l.split_whitespace().nth(1)) {
        Some(t) => t,
        None => return Ok(None),
    };
    // Prefix an origin so `Url` can parse the path-only target.
    let parsed = match Url::parse(&format!("http://127.0.0.1{target}")) {
        Ok(u) => u,
        Err(_) => return Ok(None),
    };
    let params: HashMap<String, String> = parsed
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();

    // Only the OAuth redirect carries `code` or `error`; ignore anything else
    // (favicon requests, browser preconnect probes) and keep listening.
    if !params.contains_key("code") && !params.contains_key("error") {
        let _ = stream.write_all(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n");
        return Ok(None);
    }

    let body = if params.contains_key("code") {
        RESPONSE_OK_HTML
    } else {
        RESPONSE_ERR_HTML
    };
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes());
    let _ = stream.flush();

    if let Some(err) = params.get("error") {
        return Err(format!("Google sign-in was denied or failed: {err}"));
    }
    let state = params.get("state").map(String::as_str).unwrap_or_default();
    if state != expected_state {
        return Err("OAuth state mismatch — aborting for safety.".to_string());
    }
    params
        .get("code")
        .cloned()
        .map(Some)
        .ok_or_else(|| "Redirect did not include an authorization code".to_string())
}

/// Exchange the authorization `code` for tokens at the token endpoint.
async fn exchange_code(
    token_endpoint: &str,
    token_params: &HashMap<String, String>,
    code: &str,
    redirect_uri: &str,
) -> Result<serde_json::Value, String> {
    let mut form: Vec<(&str, &str)> = token_params
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    form.push(("code", code));
    form.push(("redirect_uri", redirect_uri));

    // Encode as application/x-www-form-urlencoded via `Url` (avoids depending
    // on reqwest's optional form feature).
    let body = Url::parse_with_params("http://localhost/", &form)
        .map_err(|e| format!("Failed to encode token request: {e}"))?
        .query()
        .unwrap_or_default()
        .to_string();

    let response = reqwest::Client::new()
        .post(token_endpoint)
        .header(
            reqwest::header::CONTENT_TYPE,
            "application/x-www-form-urlencoded",
        )
        .timeout(Duration::from_secs(30))
        .body(body)
        .send()
        .await
        .map_err(|e| format!("Token exchange request failed: {e}"))?;

    let status = response.status();
    let text = response
        .text()
        .await
        .map_err(|e| format!("Failed to read token response: {e}"))?;
    if !status.is_success() {
        return Err(format!("Token endpoint returned {status}: {text}"));
    }
    serde_json::from_str(&text).map_err(|e| format!("Failed to parse token response JSON: {e}"))
}
