"""WebSocket integration tests for the live server — no browser, no network, no LLM.

Runs the REAL FastAPI app (worker included, via the TestClient lifespan) and drives
the same three sockets the browser uses, so the ingest → queue → worker → broadcast
pipeline is exercised end-to-end in-process. Skipped when the optional live extra
(fastapi/httpx) is absent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from viewlyt.live import persistence  # noqa: E402
from viewlyt.live.llm import LLMConfig  # noqa: E402
from viewlyt.live.server import LiveServer, create_app  # noqa: E402
from viewlyt.live.window import WindowConfig  # noqa: E402

YT_ORIGIN = {"origin": "https://www.youtube.com"}


@pytest.fixture(autouse=True)
def _isolated_persistence(tmp_path, monkeypatch):
    """Every control op persists — never let a test touch the real ~/.viewlyt."""
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")


def _client() -> tuple[TestClient, LiveServer]:
    server = LiveServer(LLMConfig(), WindowConfig())
    return TestClient(create_app(server)), server


def _drain_until(ws, wanted: str, limit: int = 60) -> dict:
    """Receive frames until one of type ``wanted`` arrives (bounded, not forever)."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == wanted:
            return msg
    raise AssertionError(f"no '{wanted}' frame within {limit} frames")


def _collect_chat_items(ws, count: int, limit: int = 60) -> list[dict]:
    """Accumulate feed items across 'chat' frames (the worker may split a burst
    over several ~0.25s flushes) until ``count`` arrived."""
    items: list[dict] = []
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == "chat":
            items.extend(msg["items"])
            if len(items) >= count:
                return items
    raise AssertionError(f"only {len(items)}/{count} feed items within {limit} frames")


def test_ingest_flows_to_dashboard_feed_and_counters() -> None:
    client, server = _client()
    with client:
        with client.websocket_connect("/dashboard") as dash:
            state = dash.receive_json()
            assert state["type"] == "state"
            assert state["ingested"] == 0

            with client.websocket_connect("/ingest", headers=YT_ORIGIN) as ingest:
                ingest.send_json(
                    [
                        {"author": "alice", "html": "hello <img alt=':wave:'>", "ts": 1000},
                        {"author": "", "html": "   "},  # empty after cleaning -> dropped
                        {"author": "bob", "text": "second message", "ts": 2000},
                    ]
                )
                items = _collect_chat_items(dash, 2)

            assert [i["author"] for i in items] == ["alice", "bob"]
            assert ":wave:" in items[0]["text"]

            # Counters settle at 2 (the blank row was dropped at the door).
            for _ in range(60):
                stat = _drain_until(dash, "stat")
                if stat["ingested"] == 2:
                    break
            assert stat["ingested"] == 2
    assert server.ingested == 2


def test_ingest_rejects_cross_site_origin() -> None:
    client, _ = _client()
    with client:
        # The anti-CSWSH guard closes the handshake before accepting; the client
        # sees it as a failed/denied connection (exception type varies by
        # starlette version, so assert on "it does not connect").
        with pytest.raises(Exception):  # noqa: B017
            with client.websocket_connect("/ingest", headers={"origin": "https://evil.com"}):
                raise AssertionError("cross-site origin must not connect")


def test_control_upsert_probe_broadcasts_new_state() -> None:
    client, server = _client()
    with client:
        with client.websocket_connect("/dashboard") as dash:
            assert dash.receive_json()["type"] == "state"
            with client.websocket_connect("/control") as control:
                control.send_json(
                    {
                        "op": "upsert_probe",
                        "probe": {
                            "kind": "open",
                            "id": "themes",
                            "label": "Themes",
                            "instruction": "summarize",
                        },
                    }
                )
                new_state = _drain_until(dash, "state")
            assert [p["id"] for p in new_state["probes"]] == ["themes"]
    assert "themes" in server.probes
    # The op was persisted (to the monkeypatched temp dir, never ~/.viewlyt).
    assert persistence.STATE_FILE.exists()


def test_export_endpoints_serve_downloads() -> None:
    client, _ = _client()
    with client:
        r = client.get("/export.json")
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        assert r.json()["totals"]["ingested"] == 0

        r = client.get("/export.csv")
        assert r.status_code == 200
        assert r.text.splitlines()[0].startswith("ts_utc,probe_id,")


def test_index_serves_built_dashboard() -> None:
    client, _ = _client()
    with client:
        r = client.get("/")
        assert r.status_code == 200
        assert "viewlyt" in r.text
