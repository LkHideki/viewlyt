"""Server/CLI side of the decision-model review: launch-vs-persisted precedence,
state frames, set_model coercion, client retirement.

Every test points persistence at a tmp dir (never the real ``~/.viewlyt``) and stubs
``uvicorn.run`` — no server, network or browser.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("openai")
pytest.importorskip("httpx")

from viewlyt.live import cli as live_cli  # noqa: E402
from viewlyt.live import persistence  # noqa: E402
from viewlyt.live import server as live_server  # noqa: E402
from viewlyt.live.decisions import DEFAULT_DECIDER_MODEL  # noqa: E402
from viewlyt.live.llm import HybridRunner, LLMClient, LLMConfig  # noqa: E402
from viewlyt.live.server import LiveServer, apply_control  # noqa: E402
from viewlyt.live.window import WindowConfig  # noqa: E402


def _persist_to_tmp(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")
    monkeypatch.delenv("LLM_DECIDER", raising=False)


def _launch(monkeypatch, *argv: str) -> LiveServer:
    """Run ``vl live`` main() with uvicorn stubbed; return the LiveServer it built."""
    captured: dict = {}
    real_init = LiveServer.__init__

    def _spy_init(self, *a, **k):
        real_init(self, *a, **k)
        captured["server"] = self

    monkeypatch.setattr(LiveServer, "__init__", _spy_init)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    rc = live_cli.main(
        ["--no-open", "--capture", "browser", "--api-key", "sk-launch", "--provider", "openrouter"]
        + list(argv)
    )
    assert rc == 0
    return captured["server"]


# --- item 1: precedence ----------------------------------------------------------------


def test_launch_decider_wins_over_the_persisted_one_and_can_be_turned_off(
    tmp_path, monkeypatch
) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    # Launch 1: no flag -> off, and the state file is seeded.
    s1 = _launch(monkeypatch)
    assert s1.llm_cfg.decider_model == ""
    assert type(s1.client()) is LLMClient
    assert persistence.STATE_FILE.exists()
    # Launch 2: --decider must beat the seeded (empty) persisted value.
    s2 = _launch(monkeypatch, "--decider")
    assert s2.llm_cfg.decider_model == DEFAULT_DECIDER_MODEL
    assert isinstance(s2.client(), HybridRunner)
    # Launch 3: --no-decider must clear the saved decider.
    s3 = _launch(monkeypatch, "--no-decider")
    assert s3.llm_cfg.decider_model == ""
    assert type(s3.client()) is LLMClient
    # Launch 4: no flag keeps what the last explicit choice persisted (off).
    assert _launch(monkeypatch).llm_cfg.decider_model == ""


def test_no_flag_keeps_the_saved_decider(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    _launch(monkeypatch, "--decider", "perplexity/pplx-decider-latest")
    s = _launch(monkeypatch)
    assert s.llm_cfg.decider_model == "perplexity/pplx-decider-latest"
    assert isinstance(s.client(), HybridRunner)


def test_env_llm_decider_behaves_like_the_flag(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    _launch(monkeypatch)  # seed with no decider
    monkeypatch.setenv("LLM_DECIDER", "env/decider-model")
    assert _launch(monkeypatch).llm_cfg.decider_model == "env/decider-model"
    # ...and an explicit --no-decider still beats the environment.
    assert _launch(monkeypatch, "--no-decider").llm_cfg.decider_model == ""


def test_run_without_override_keeps_persisted_and_with_override_replaces(
    tmp_path, monkeypatch
) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: None)
    persistence.save_state(
        {"n": 230, "gap": 45.0, "mode": "hybrid", "capacity": 3000},
        {
            "base_url": "https://openrouter.ai/api/v1",
            "model": "m",
            "api_key": "",
            "decider_model": "saved/decider",
        },
        [],
    )
    cfg = LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="sk-launch", model="m")
    seen: list[LiveServer] = []
    real_init = LiveServer.__init__

    def _spy_init(self, *a, **k):
        real_init(self, *a, **k)
        seen.append(self)

    monkeypatch.setattr(LiveServer, "__init__", _spy_init)
    live_server.run(host="127.0.0.1", llm_cfg=cfg)
    assert seen[-1].llm_cfg.decider_model == "saved/decider"
    live_server.run(host="127.0.0.1", llm_cfg=cfg, decider_override="")
    assert seen[-1].llm_cfg.decider_model == ""
    assert persistence.load_state()["model"]["decider_model"] == ""  # type: ignore[index]


def test_startup_line_reports_the_effective_decider(tmp_path, monkeypatch, capsys) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    _launch(monkeypatch, "--no-decider")
    assert "decider:      off" in capsys.readouterr().out
    _launch(monkeypatch, "--decider")
    assert f"decider:      {DEFAULT_DECIDER_MODEL} (classification)" in capsys.readouterr().out
    # Configured but the provider has no decisions endpoint: say it is inactive and why.
    persistence.STATE_FILE.unlink()  # a saved base_url would otherwise beat --provider
    _launch(monkeypatch, "--decider", "--provider", "groq")
    out = capsys.readouterr().out
    assert "INACTIVE" in out and "no decisions endpoint" in out


# --- item 6: state frame ---------------------------------------------------------------


def test_state_message_reports_decider_activity_and_health() -> None:
    srv = LiveServer(
        LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="k"), WindowConfig()
    )
    msg = srv.state_message()
    assert msg["decider_state"] == "off" and msg["model"]["decider_active"] is False

    srv.llm_cfg = LLMConfig(
        base_url="https://openrouter.ai/api/v1", api_key="k", decider_model="x/d"
    )
    assert srv.state_message()["decider_state"] == "ok"
    assert srv.state_message()["model"]["decider_active"] is True
    runner = srv.client()
    assert isinstance(runner, HybridRunner)
    runner._failures = 1
    assert srv.state_message()["decider_state"] == "degraded"
    runner._failures = 3
    runner._open_until = runner._clock() + 100
    assert srv.state_message()["decider_state"] == "open"

    srv.llm_cfg = LLMConfig(base_url="http://localhost:1234/v1", decider_model="x/d")
    srv._client = None
    msg = srv.state_message()
    assert msg["decider_state"] == "inactive" and msg["model"]["decider_active"] is False


# --- item 8: set_model type coercion ---------------------------------------------------


@pytest.mark.parametrize("bad", [None, 123, {"a": 1}, ["x"], True])
def test_set_model_ignores_a_non_string_decider(tmp_path, monkeypatch, bad) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    srv = LiveServer(LLMConfig(api_key="k"), WindowConfig())
    asyncio.run(apply_control(srv, {"op": "set_model", "decider_model": bad}))
    assert srv.llm_cfg.decider_model == ""  # not "None" / "123" / "{'a': 1}"
    srv.llm_cfg = LLMConfig(api_key="k", decider_model="keep/me")
    asyncio.run(apply_control(srv, {"op": "set_model", "decider_model": bad}))
    assert srv.llm_cfg.decider_model == "keep/me"


def test_set_model_strips_a_string_decider(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)
    srv = LiveServer(LLMConfig(api_key="k"), WindowConfig())
    asyncio.run(apply_control(srv, {"op": "set_model", "decider_model": "  a/b  "}))
    assert srv.llm_cfg.decider_model == "a/b"
    asyncio.run(apply_control(srv, {"op": "set_model", "decider_model": "   "}))
    assert srv.llm_cfg.decider_model == ""


# --- item 11: retire the replaced client -----------------------------------------------


class _Closable:
    model = "m"

    def __init__(self, fail: bool = False) -> None:
        self.closed = 0
        self.fail = fail

    async def aclose(self) -> None:
        self.closed += 1
        if self.fail:
            raise RuntimeError("close failed")


@pytest.mark.parametrize("op", [{"op": "set_model", "model": "other"}, {"op": "reset_state"}])
def test_replaced_client_is_closed_in_the_background(tmp_path, monkeypatch, op) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)

    async def go() -> None:
        srv = LiveServer(LLMConfig(api_key="k"), WindowConfig())
        old = _Closable()
        srv._client = old  # type: ignore[assignment]
        await apply_control(srv, op)
        assert srv._client is None
        await asyncio.gather(*srv._bg_tasks)
        assert old.closed == 1

    asyncio.run(go())


def test_a_failing_close_never_reaches_the_control_op(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)

    async def go() -> None:
        srv = LiveServer(LLMConfig(api_key="k"), WindowConfig())
        old = _Closable(fail=True)
        srv._client = old  # type: ignore[assignment]
        await apply_control(srv, {"op": "set_model", "model": "other"})
        await asyncio.gather(*srv._bg_tasks)
        assert old.closed == 1 and srv.llm_cfg.model == "other"

    asyncio.run(go())


def test_replaced_client_waits_for_the_in_flight_batch(tmp_path, monkeypatch) -> None:
    _persist_to_tmp(tmp_path, monkeypatch)

    async def go() -> None:
        srv = LiveServer(LLMConfig(api_key="k"), WindowConfig())
        gate = asyncio.Event()
        srv.current_batch_task = asyncio.create_task(gate.wait())
        old = _Closable()
        srv._client = old  # type: ignore[assignment]
        await apply_control(srv, {"op": "set_model", "model": "other"})
        for _ in range(5):
            await asyncio.sleep(0)
        assert old.closed == 0  # the running window still uses it
        gate.set()
        await asyncio.gather(*srv._bg_tasks)
        assert old.closed == 1

    asyncio.run(go())


def test_llm_client_aclose_closes_the_openai_client() -> None:
    async def go() -> None:
        client = LLMClient(LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="k"))
        closed: list[int] = []

        class _Inner:
            async def close(self) -> None:
                closed.append(1)

        client._client = _Inner()  # type: ignore[assignment]
        await client.aclose()
        assert closed == [1]
        client._client = object()  # type: ignore[assignment]
        await client.aclose()  # no close/aclose attribute: skipped silently

    asyncio.run(go())
