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
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger("viewlyt.live")

STATE_DIR = Path.home() / ".viewlyt"
STATE_FILE = STATE_DIR / "live-state.json"
KEY_FILE = STATE_DIR / "key"


_FERNETS: dict[Path, Fernet] = {}


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path``, creating it mode 0600 **atomically**.

    ``os.open`` with the mode argument narrows the new file's permissions before
    any bytes exist, closing the create-then-``chmod`` TOCTOU window (CWE-367/276)
    where a racing local user could read the plaintext key/state while the file was
    briefly world-readable. The trailing ``chmod`` also tightens a legacy file that
    an older version created 0644.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    path.chmod(0o600)


def _fernet() -> Fernet:
    """Return a Fernet bound to ``KEY_FILE``, generating the key on first use.

    Cached per key path: save/load run on every control op, and re-reading +
    re-deriving the key each time is wasted I/O (tests monkeypatch ``KEY_FILE``,
    hence a dict keyed by path instead of a single memo).
    """
    cached = _FERNETS.get(KEY_FILE)
    if cached is not None:
        return cached
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not KEY_FILE.exists():
        _write_private(KEY_FILE, Fernet.generate_key())
    f = Fernet(KEY_FILE.read_bytes())
    _FERNETS[KEY_FILE] = f
    return f


def save_state(window: dict, model: dict, probes: list[dict]) -> None:
    """Persist the window/model/probes to ``STATE_FILE`` with the API key encrypted."""
    try:
        payload = {
            "window": window,
            "model": {
                "base_url": model["base_url"],
                "model": model["model"],
                "budget": float(model.get("budget", 0.0)),
                "language": str(model.get("language") or "Portuguese (Brazil)"),
                "api_key_enc": _fernet().encrypt(str(model.get("api_key") or "").encode()).decode(),
            },
            "probes": probes,
        }
        _write_private(STATE_FILE, json.dumps(payload).encode("utf-8"))
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
                "language": str(model.get("language") or "Portuguese (Brazil)"),
                "api_key": api_key,
            },
            "probes": list(payload.get("probes", [])),
        }
    except Exception:
        logger.warning("could not load live state", exc_info=True)
        return None
