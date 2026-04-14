"""Firebase Admin SDK initialization — runs once, resolves credentials."""

from __future__ import annotations

import glob
import json
import logging
import os

import firebase_admin
from firebase_admin import credentials

log = logging.getLogger(__name__)

_initialized = False


def _resolve_service_account_path() -> str | None:
    explicit = os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()
    if explicit:
        if os.path.isabs(explicit):
            return explicit if os.path.isfile(explicit) else None
        base = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        candidate = os.path.join(base, explicit)
        return candidate if os.path.isfile(candidate) else None

    creds_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "creds")
    pattern = os.path.join(creds_dir, "*-adminsdk-*.json")
    matches = sorted(glob.glob(pattern))
    return matches[0] if matches else None


def init_firebase() -> None:
    global _initialized
    if _initialized:
        return

    try:
        firebase_admin.get_app()
        _initialized = True
        return
    except ValueError:
        pass

    sa_path = _resolve_service_account_path()
    if sa_path:
        cred = credentials.Certificate(sa_path)
        firebase_admin.initialize_app(cred)
        log.info("Firebase initialized from service account file: %s", sa_path)
    else:
        sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        if sa_json:
            cred = credentials.Certificate(json.loads(sa_json))
            firebase_admin.initialize_app(cred)
            log.info("Firebase initialized from FIREBASE_SERVICE_ACCOUNT_JSON")
        else:
            firebase_admin.initialize_app()
            log.info("Firebase initialized with Application Default Credentials")

    _initialized = True
