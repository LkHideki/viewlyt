"""On-disk persistence for the live server's config + probes (API key encrypted).

The server's window/model/probes are saved to ``~/.viewlyt/live-state.json`` so a
restart resumes where it left off. The model's ``api_key`` is never written in the
clear: it is Fernet-encrypted with a key kept in ``~/.viewlyt/key`` (chmod 600).
Persistence is best-effort — every entry point swallows and logs failures so a
disk/permission problem can never crash the app. Stdlib + cryptography only; no
FastAPI, no Selenium.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger("viewlyt.live")

STATE_DIR = Path.home() / ".viewlyt"
STATE_FILE = STATE_DIR / "live-state.json"
KEY_FILE = STATE_DIR / "key"


def _fernet() -> Fernet:
    """Return a Fernet bound to ``KEY_FILE``, generating the key on first use."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(Fernet.generate_key())
        KEY_FILE.chmod(0o600)
    return Fernet(KEY_FILE.read_bytes())


def save_state(window: dict, model: dict, probes: list[dict]) -> None:
    """Persist the window/model/probes to ``STATE_FILE`` with the API key encrypted."""
    try:
        payload = {
            "window": window,
            "model": {
                "base_url": model["base_url"],
                "model": model["model"],
                "budget": float(model.get("budget", 0.0)),
                "api_key_enc": _fernet().encrypt(str(model.get("api_key") or "").encode()).decode(),
            },
            "probes": probes,
        }
        STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
        STATE_FILE.chmod(0o600)
    except Exception:
        logger.warning("could not persist live state", exc_info=True)


def load_state() -> dict | None:
    """Load and decrypt the saved state, or ``None`` if missing/unreadable."""
    if not STATE_FILE.exists():
        return None
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        model = payload["model"]
        api_key = _fernet().decrypt(model["api_key_enc"].encode()).decode()
        return {
            "window": payload["window"],
            "model": {
                "base_url": model["base_url"],
                "model": model["model"],
                "budget": float(model.get("budget", 0.0)),
                "api_key": api_key,
            },
            "probes": list(payload.get("probes", [])),
        }
    except Exception:
        logger.warning("could not load live state", exc_info=True)
        return None
