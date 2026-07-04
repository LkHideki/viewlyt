"""Server-side behaviors of the live dashboard: broadcast fan-out, history, export.

Uses fake WebSocket / LLM objects so no real server, network, or LLM is involved.
Skipped when the optional FastAPI dep (``viewlyt[live]``) is absent.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")

from viewlyt.live import server as live_server  # noqa: E402
from viewlyt.live.llm import LLMConfig  # noqa: E402
from viewlyt.live.messages import ChatMessage  # noqa: E402
from viewlyt.live.probes import ClassificationProbe, OpenSummaryProbe, Probe  # noqa: E402
from viewlyt.live.server import (  # noqa: E402
    ConnectionManager,
    LiveServer,
    apply_control,
    export_csv,
    export_payload,
    process_window,
)
from viewlyt.live.window import WindowConfig  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class BrokenWS(FakeWS):
    async def send_text(self, payload: str) -> None:
        raise RuntimeError("send failed")


class StuckWS(FakeWS):
    async def send_text(self, payload: str) -> None:
        await asyncio.sleep(60)


def test_broadcast_sends_the_same_payload_to_all() -> None:
    mgr = ConnectionManager()
    a, b = FakeWS(), FakeWS()
    mgr.active = {a, b}  # type: ignore[assignment]
    asyncio.run(mgr.broadcast({"type": "stat", "ingested": 7}))
    assert json.loads(a.sent[0]) == {"type": "stat", "ingested": 7}
    assert a.sent == b.sent


def test_broadcast_drops_a_broken_socket_and_keeps_the_rest() -> None:
    mgr = ConnectionManager()
    ok, broken = FakeWS(), BrokenWS()
    mgr.active = {ok, broken}  # type: ignore[assignment]
    asyncio.run(mgr.broadcast({"t": 1}))
    assert broken not in mgr.active
    assert ok in mgr.active
    assert len(ok.sent) == 1


def test_broadcast_times_out_a_stuck_socket_without_stalling(monkeypatch) -> None:
    # One hung tab must not delay the healthy one (concurrent sends) nor block the
    # caller for its full sleep (timeout kicks in and the socket is dropped).
    monkeypatch.setattr(live_server, "_SEND_TIMEOUT", 0.05)
    mgr = ConnectionManager()
    ok, stuck = FakeWS(), StuckWS()
    mgr.active = {ok, stuck}  # type: ignore[assignment]
    t0 = time.monotonic()
    asyncio.run(mgr.broadcast({"t": 1}))
    assert time.monotonic() - t0 < 1.0
    assert stuck not in mgr.active
    assert ok in mgr.active
    assert len(ok.sent) == 1


# ---------------------------------------------------------------------------
# Result history + backfill + export
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Labels every message with the probe's first category / a fixed summary."""

    model = "fake"
    total_tokens = 0
    total_cost = 0.0

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        if probe.kind == "classification":
            cats = probe.categories  # type: ignore[attr-defined]
            return {"labels": [cats[0] for _ in messages]}
        return {"summary": "all good, mood: hyped"}


def _server_with_history() -> LiveServer:
    srv = LiveServer(LLMConfig(), WindowConfig())
    srv._client = FakeLLMClient()  # type: ignore[assignment]
    srv.probes["mood"] = ClassificationProbe(
        id="mood", label="Mood, live", question="q", categories=["happy", "sad"]
    )
    srv.probes["sum"] = OpenSummaryProbe(id="sum", label="Summary", instruction="sum it")
    msgs = [ChatMessage(author=f"u{i}", text=f"m{i}", ts=float(i)) for i in range(4)]
    asyncio.run(process_window(srv, msgs, 1_700_000_000.0))
    return srv


def test_process_window_appends_snapshots_to_history() -> None:
    srv = _server_with_history()
    assert [d["ts"] for d in srv.history["mood"]] == [1_700_000_000.0]
    assert srv.history["mood"][0]["pct"]["happy"] == 100.0
    assert srv.history["sum"][0]["text"] == "all good, mood: hyped"


def test_state_message_carries_runtime_flags_and_history_message_backfills() -> None:
    srv = _server_with_history()
    srv.processing = True
    srv.budget_blocked = True
    state = srv.state_message()
    assert state["processing"] is True
    assert state["budget_blocked"] is True
    assert "tokens_total" in state and "cost_total" in state
    hist = srv.history_message()
    assert hist["type"] == "history"
    assert set(hist["probes"]) == {"mood", "sum"}
    assert hist["probes"]["mood"][0]["type"] == "result"


def test_export_payload_shape() -> None:
    srv = _server_with_history()
    payload = export_payload(srv)
    assert payload["totals"]["tokens"] == srv.total_tokens
    by_id = {entry["probe"]["id"]: entry for entry in payload["probes"]}
    assert set(by_id) == {"mood", "sum"}
    assert len(by_id["mood"]["history"]) == 1
    json.dumps(payload)  # must be JSON-serializable as-is


def test_export_csv_flattens_categories_and_text() -> None:
    srv = _server_with_history()
    lines = export_csv(srv).splitlines()
    assert lines[0] == "ts_utc,probe_id,kind,label,n,category,pct,text"
    body = "\n".join(lines[1:])
    assert '"Mood, live"' in body  # comma-carrying label is properly quoted
    assert ",happy,100.0," in body
    assert ",sad,0.0," in body
    assert "all good, mood: hyped" in body


# ---------------------------------------------------------------------------
# Capture snippet / userscript / extension (regression: Safari host fallback)
# ---------------------------------------------------------------------------


def test_render_snippet_binds_host_port_and_rotates_loopback_hosts() -> None:
    js = live_server.render_snippet("127.0.0.1", 8123)
    assert "%HOST%" not in js and "%PORT%" not in js
    assert '"8123"' in js
    # Safari fix: loopback must offer BOTH spellings (mixed-content exemption is
    # hostname-based on WebKit), and the code must rotate between them.
    assert 'return [h, "localhost"]' in js
    assert 'return [h, "127.0.0.1"]' in js
    assert "hostIdx" in js


def test_render_snippet_custom_host_has_no_loopback_fallback() -> None:
    js = live_server.render_snippet("192.168.0.10", 8000)
    assert '"192.168.0.10"' in js
    # A LAN host must not silently fall back to the developer's own machine.
    assert "localhost:8000" not in js


def test_userscript_wraps_snippet_with_metadata() -> None:
    us = live_server.render_userscript("127.0.0.1", 8000)
    assert us.startswith("// ==UserScript==")
    assert "@match        https://www.youtube.com/live_chat*" in us
    assert "/ingest" in us


def test_extension_zip_is_valid_and_carries_the_snippet() -> None:
    import io as _io
    import zipfile as _zipfile

    blob = live_server.build_extension_zip("127.0.0.1", 8000)
    with _zipfile.ZipFile(_io.BytesIO(blob)) as zf:
        names = set(zf.namelist())
        assert names == {"manifest.json", "content.js"}
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["manifest_version"] == 3
        content = zf.read("content.js").decode()
        assert content == live_server.render_snippet("127.0.0.1", 8000)


def test_partial_probe_failure_is_reported_by_name() -> None:
    # One probe failing while others succeed must produce a named error frame —
    # previously the card just silently stopped updating.
    class Flaky(FakeLLMClient):
        async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
            if probe.id == "boom":
                raise RuntimeError("simulated provider error")
            return await super().run(probe, messages)

    srv = LiveServer(LLMConfig(), WindowConfig())
    srv._client = Flaky()  # type: ignore[assignment]
    srv.probes["ok"] = OpenSummaryProbe(id="ok", label="OK", instruction="i")
    srv.probes["boom"] = OpenSummaryProbe(id="boom", label="Boom", instruction="i")
    ws = FakeWS()
    srv.dash.active = {ws}  # type: ignore[assignment]

    msgs = [ChatMessage(author="u", text="m", ts=1.0)]
    asyncio.run(process_window(srv, msgs, 1.0))

    frames = [json.loads(s) for s in ws.sent]
    assert any(f["type"] == "result" and f["probe_id"] == "ok" for f in frames)
    errors = [f["message"] for f in frames if f["type"] == "error"]
    assert any("Boom" in m for m in errors)


def test_remove_probe_and_reset_state_drop_history(tmp_path, monkeypatch) -> None:
    # apply_control persists state-changing ops — point it at a temp dir so the
    # test never touches the real ~/.viewlyt.
    from viewlyt.live import persistence

    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")

    srv = _server_with_history()
    asyncio.run(apply_control(srv, {"op": "remove_probe", "id": "mood"}))
    assert "mood" not in srv.history
    asyncio.run(apply_control(srv, {"op": "reset_state"}))
    assert srv.history == {}
