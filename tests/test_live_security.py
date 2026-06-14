"""WebSocket Origin allow-list (anti-CSWSH) for the live server.

Guards the rule that a cross-site browser Origin cannot open /control or /dashboard
(which would let a malicious tab redirect the LLM base_url and exfiltrate the API
key). Skipped when the optional FastAPI dep (``viewlyt[live]``) is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from viewlyt.live.server import _origin_allowed  # noqa: E402

HOST, PORT = "127.0.0.1", 8000


def test_missing_origin_is_allowed() -> None:
    # Non-browser clients (curl, a local script) send no Origin and can't mount CSWSH.
    assert _origin_allowed(None, HOST, PORT, allow_youtube=False) is True
    assert _origin_allowed("", HOST, PORT, allow_youtube=True) is True


def test_same_origin_dashboard_allowed() -> None:
    for o in ("http://127.0.0.1:8000", "http://localhost:8000"):
        assert _origin_allowed(o, HOST, PORT, allow_youtube=False) is True


def test_cross_site_origin_rejected_on_control() -> None:
    # The CSWSH vector: a malicious page trying to reach /control or /dashboard.
    for o in ("https://evil.com", "http://evil.com:8000", "https://www.youtube.com"):
        assert _origin_allowed(o, HOST, PORT, allow_youtube=False) is False


def test_youtube_allowed_only_for_ingest() -> None:
    assert _origin_allowed("https://www.youtube.com", HOST, PORT, allow_youtube=True) is True
    assert _origin_allowed("https://m.youtube.com", HOST, PORT, allow_youtube=True) is True
    # Look-alike host must NOT match the suffix check.
    assert _origin_allowed("https://evil-youtube.com", HOST, PORT, allow_youtube=True) is False
    assert (
        _origin_allowed("http://www.youtube.com", HOST, PORT, allow_youtube=True) is False
    )  # not https


def test_wrong_port_rejected() -> None:
    assert _origin_allowed("http://127.0.0.1:9999", HOST, PORT, allow_youtube=False) is False
