"""Firebase Admin SDK: ID token verification and Firestore user profile operations."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import auth as _fb_auth
from firebase_admin import credentials, firestore
from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def _resolve_service_account_path() -> Path | None:
    configured_path = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH")
    if configured_path:
        candidate = Path(configured_path)
        if not candidate.is_absolute():
            candidate = Path(__file__).resolve().parent / candidate
        if candidate.is_file():
            return candidate
        log.warning("FIREBASE_SERVICE_ACCOUNT_PATH not found: %s", candidate)

    creds_dir = Path(__file__).resolve().parent / "creds"
    matches = sorted(creds_dir.glob("*-adminsdk-*.json"))
    return matches[0] if matches else None


def _init_firebase() -> None:
    if firebase_admin._apps:
        return
    sa_path = _resolve_service_account_path()
    if sa_path:
        cred = credentials.Certificate(str(sa_path))
        firebase_admin.initialize_app(cred)
        log.info("Firebase Admin SDK initialised from service account file: %s", sa_path)
        return
    sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        cred = credentials.Certificate(json.loads(sa_json))
        firebase_admin.initialize_app(cred)
        log.info("Firebase Admin SDK initialised from FIREBASE_SERVICE_ACCOUNT_JSON")
    else:
        # Application Default Credentials — works automatically on Cloud Run
        firebase_admin.initialize_app()
        log.info("Firebase Admin SDK initialised from Application Default Credentials")


_init_firebase()


# ---------------------------------------------------------------------------
# FastAPI auth dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> dict:
    """Verify a Firebase ID token. Returns {uid, email}."""
    token = creds.credentials if creds and creds.credentials else request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing auth token")
    try:
        decoded = _fb_auth.verify_id_token(token)
        return {"uid": decoded["uid"], "email": decoded.get("email")}
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")



# ---------------------------------------------------------------------------
# Firestore profile helpers
# ---------------------------------------------------------------------------

def _db():
    return firestore.client()


def get_user_profile(uid: str) -> dict | None:
    doc = _db().collection("users").document(uid).get()
    return doc.to_dict() if doc.exists else None


def create_user_profile(uid: str, email: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    profile = {
        "display_name": email.split("@")[0] if email else "User",
        "avatar_url": None,
        "billing_plan": "free",
        "render_credits": 100,
        "created_at": now,
        "updated_at": now,
    }
    _db().collection("users").document(uid).set(profile)
    log.info(f"Created Firestore profile for uid={uid}")
    return profile


def update_user_profile(uid: str, updates: dict) -> dict:
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    _db().collection("users").document(uid).update(updates)
    return updates


def get_or_create_profile(uid: str, email: str | None) -> dict:
    return get_user_profile(uid) or create_user_profile(uid, email)


def write_job_record(uid: str, job_id: str, data: dict) -> None:
    _db().collection("users").document(uid).collection("jobs").document(job_id).set(data)


def write_render_group_record(uid: str, group_id: str, data: dict) -> None:
    _db().collection("users").document(uid).collection("render_groups").document(group_id).set(data)
