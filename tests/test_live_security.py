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


def test_literal_null_origin_rejected() -> None:
    # Safari/sandboxed iframes send the literal string "null" as the Origin; it is
    # a browser context (CSWSH applies), so it must NOT pass the no-Origin exemption.
    assert _origin_allowed("null", HOST, PORT, allow_youtube=False) is False
    assert _origin_allowed("null", HOST, PORT, allow_youtube=True) is False


# ---------------------------------------------------------------------------
# Host pin (anti-DNS-rebinding): a rebound attacker hostname is same-origin with
# itself, so the Origin check alone would let it through.
# ---------------------------------------------------------------------------
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def test_host_allowed_loopback_names() -> None:
    from viewlyt.live.server import _host_allowed

    for h in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST", "127.0.0.1"):
        assert _host_allowed(h, LOOPBACK) is True


def test_host_rejects_rebound_or_missing_host() -> None:
    from viewlyt.live.server import _host_allowed

    for h in ("evil.test:8000", "127.0.0.1.evil.test", "", None, "[bad"):
        assert _host_allowed(h, LOOPBACK) is False
    # Explicit wildcard insecure bind disables the pin.
    assert _host_allowed("evil.test", None) is True


def test_rebound_host_blocked_on_http_and_ws(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from viewlyt.live import persistence
    from viewlyt.live.llm import LLMConfig
    from viewlyt.live.server import LiveServer, create_app
    from viewlyt.live.window import WindowConfig

    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")
    app = create_app(LiveServer(LLMConfig(), WindowConfig()))

    evil = TestClient(app, base_url="http://evil.test:8000")
    assert evil.get("/export.json").status_code == 421
    for path in ("/control", "/dashboard", "/ingest"):
        with pytest.raises(WebSocketDisconnect):
            with evil.websocket_connect(
                f"ws://evil.test:8000{path}", headers={"origin": "http://evil.test:8000"}
            ):
                raise AssertionError(f"rebound host must not open {path}")

    ok = TestClient(app, base_url="http://127.0.0.1:8000")
    assert ok.get("/export.json").status_code == 200
