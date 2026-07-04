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


# ---------------------------------------------------------------------------
# Per-probe sample_n + probe decomposition
# ---------------------------------------------------------------------------


class RecordingClient(FakeLLMClient):
    """Records how many messages each probe actually saw."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        self.seen[probe.id] = len(messages)
        return await super().run(probe, messages)


def test_process_window_slices_per_probe_sample_n() -> None:
    # Global n=3; the probe with sample_n=2 sees its own smaller tail slice.
    srv = LiveServer(LLMConfig(), WindowConfig(n=3))
    client = RecordingClient()
    srv._client = client  # type: ignore[assignment]
    srv.probes["full"] = OpenSummaryProbe(id="full", label="Full", instruction="i")
    small = OpenSummaryProbe(id="small", label="Small", instruction="i")
    small.sample_n = 2
    srv.probes["small"] = small

    msgs = [ChatMessage(author=f"u{i}", text=f"m{i}", ts=float(i)) for i in range(4)]
    asyncio.run(process_window(srv, msgs, 1.0))
    assert client.seen == {"full": 3, "small": 2}


def test_process_window_probes_subset_only_runs_those() -> None:
    srv = LiveServer(LLMConfig(), WindowConfig(n=3))
    client = RecordingClient()
    srv._client = client  # type: ignore[assignment]
    srv.probes["a"] = OpenSummaryProbe(id="a", label="A", instruction="i")
    srv.probes["b"] = OpenSummaryProbe(id="b", label="B", instruction="i")

    msgs = [ChatMessage(author="u", text="m", ts=1.0)]
    asyncio.run(process_window(srv, msgs, 1.0, probes=[srv.probes["a"]]))
    assert set(client.seen) == {"a"}
    assert "b" not in srv.history  # the skipped probe reports no failure either


class DecomposingClient(FakeLLMClient):
    """complete_json returns a fixed composite decomposition."""

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return {
            "rationale": "composite: quantify + explain",
            "is_composite": True,
            "probes": [
                {
                    "kind": "classification",
                    "label": "Tech problems",
                    "question": "Is this message reporting a technical problem?",
                    "categories": ["yes", "no"],
                },
                {"kind": "open", "label": "Tech summary", "instruction": "summarize problems"},
            ],
        }


class FailingClient(FakeLLMClient):
    async def complete_json(self, system: str, user: str, schema: dict) -> dict:
        raise RuntimeError("llm down")


def test_decompose_broadcasts_suggestions_with_unique_ids() -> None:
    srv = LiveServer(LLMConfig(), WindowConfig())
    srv._client = DecomposingClient()  # type: ignore[assignment]
    ws = FakeWS()
    srv.dash.active = {ws}  # type: ignore[assignment]

    asyncio.run(live_server._decompose(srv, "technical problems"))

    frames = [json.loads(s) for s in ws.sent]
    sugg = [f for f in frames if f["type"] == "suggestions"]
    assert len(sugg) == 1
    probes = sugg[0]["probes"]
    assert {p["kind"] for p in probes} == {"classification", "open"}
    ids = [p["id"] for p in probes]
    assert len(ids) == len(set(ids)) and all(ids)
    # Specs are proposals: nothing was added or persisted.
    assert srv.probes == {}


def test_decompose_failure_broadcasts_error_frame() -> None:
    srv = LiveServer(LLMConfig(), WindowConfig())
    srv._client = FailingClient()  # type: ignore[assignment]
    ws = FakeWS()
    srv.dash.active = {ws}  # type: ignore[assignment]

    asyncio.run(live_server._decompose(srv, "anything"))

    frames = [json.loads(s) for s in ws.sent]
    assert any(f["type"] == "error" for f in frames)
    assert not any(f["type"] == "suggestions" for f in frames)


# ---------------------------------------------------------------------------
# Security regressions (secperf S3): set_model must not exfiltrate the stored
# API key by carrying it over to an attacker-chosen base_url.
# ---------------------------------------------------------------------------
def _persist_to_tmp(tmp_path, monkeypatch) -> None:
    from viewlyt.live import persistence

    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")


def test_set_model_drops_key_when_base_url_is_unknown(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    srv = LiveServer(
        LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="sk-secret", model="m"),
        WindowConfig(),
    )
    # An attacker-reachable /control op repoints base_url to a host they own, no key.
    asyncio.run(apply_control(srv, {"op": "set_model", "base_url": "https://evil.example/v1"}))
    assert srv.llm_cfg.base_url == "https://evil.example/v1"
    assert srv.llm_cfg.api_key == "", "stored key must NOT be forwarded to an unknown host"


def test_set_model_keeps_key_for_same_or_known_provider(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    srv = LiveServer(
        LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="sk-secret", model="m"),
        WindowConfig(),
    )
    # Same base_url (just a model swap) keeps the key.
    asyncio.run(apply_control(srv, {"op": "set_model", "model": "other"}))
    assert srv.llm_cfg.api_key == "sk-secret"
    # Switching to a RECOGNIZED provider (in PROVIDERS) also keeps it — expected UX.
    asyncio.run(apply_control(srv, {"op": "set_model", "base_url": "https://api.openai.com/v1"}))
    assert srv.llm_cfg.api_key == "sk-secret"


def test_set_model_uses_explicit_key_for_unknown_host(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    srv = LiveServer(LLMConfig(api_key="sk-old"), WindowConfig())
    asyncio.run(
        apply_control(
            srv,
            {"op": "set_model", "base_url": "https://custom.local/v1", "api_key": "sk-new"},
        )
    )
    assert srv.llm_cfg.api_key == "sk-new"


# ---------------------------------------------------------------------------
# Security regression (secperf S1): the CORS/PNA middleware must NOT emit a
# blanket Access-Control-Allow-Origin:* that lets any site read /export.*.
# ---------------------------------------------------------------------------
def test_cors_headers_only_for_youtube_origin() -> None:
    from fastapi.testclient import TestClient

    from viewlyt.live.server import create_app

    client = TestClient(create_app(LiveServer(LLMConfig(), WindowConfig())))

    # A foreign website must get NO cross-origin grant on the session export.
    r = client.get("/export.json", headers={"origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    # The youtube popout (the only legit cross-origin caller) gets its exact origin
    # echoed plus the Private-Network grant — never "*".
    yt = "https://www.youtube.com"
    r2 = client.get("/export.json", headers={"origin": yt})
    assert r2.headers.get("access-control-allow-origin") == yt
    assert r2.headers.get("access-control-allow-private-network") == "true"


# ---------------------------------------------------------------------------
# Security regression (secperf S2): run() must refuse a non-loopback bind unless
# the operator explicitly opts in — the server has no authentication.
# ---------------------------------------------------------------------------
def test_run_refuses_non_loopback_bind_without_flag(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        live_server.run(host="0.0.0.0", open_browser=False)


def test_run_allows_non_loopback_bind_with_explicit_flag(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    served: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(kw))
    live_server.run(host="0.0.0.0", open_browser=False, allow_insecure_bind=True)
    assert served.get("host") == "0.0.0.0"  # reached uvicorn instead of refusing


def test_run_serves_loopback_by_default(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    served: dict = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(kw))
    live_server.run(host="127.0.0.1", open_browser=False)
    assert served.get("host") == "127.0.0.1"
