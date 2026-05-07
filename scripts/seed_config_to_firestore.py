"""Seed Firestore ``config/global`` from the bundled ``serverV2/config.json``.

One-shot CLI.  Run after editing the bundled defaults to push them into
Firestore, or once on first deploy to populate the doc.

Usage:
    python -m scripts.seed_config_to_firestore

Validates the JSON shape by parsing it through ``AppConfig.from_dict``
before writing -- if the bundled file is broken, the script fails loud
and Firestore is left untouched.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from firebase_admin import firestore

from serverV2.allocation.render_config import RenderConfig
from serverV2.infrastructure.auth.firebase_app import init_firebase


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_config")

_BUNDLED_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "serverV2",
    "config.json",
)
_COLLECTION = "config"
_DOC_ID = "global"
_FIELD = "json"


def main() -> int:
    log.info("Reading bundled config from %s", _BUNDLED_PATH)
    with open(_BUNDLED_PATH, "r") as f:
        raw = f.read()
    parsed = json.loads(raw)

    log.info("Validating shape via RenderConfig.from_dict ...")
    RenderConfig.from_dict(parsed)  # raises on missing/invalid keys
    log.info("Shape OK.")

    init_firebase()
    client = firestore.client()
    log.info("Writing to Firestore %s/%s", _COLLECTION, _DOC_ID)
    client.collection(_COLLECTION).document(_DOC_ID).set({_FIELD: raw})
    log.info("Done.  Firestore now holds the bundled config.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
