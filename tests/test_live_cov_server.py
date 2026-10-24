"""Behavior/regression tests for ``viewlyt.live.server`` (scheduling, control ops, routes).

Everything is in-process and offline: LLM runners are fakes, the worker runs against a
fake queue and a fake monotonic clock (so ticks are ~1 ms and nothing sleeps), the
routes go through ``TestClient``, and ``run()`` is exercised with ``uvicorn.run``
stubbed. Persistence is always redirected to ``tmp_path`` (never the real ``~/.viewlyt``).
Skipped when the optional live extra (fastapi/httpx/cryptography) is absent.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
import zipfile
from collections import deque
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from viewlyt.live import persistence  # noqa: E402
from viewlyt.live import server as live_server  # noqa: E402
from viewlyt.live.llm import HybridRunner, LLMConfig  # noqa: E402
from viewlyt.live.messages import ChatMessage  # noqa: E402
from viewlyt.live.probes import ClassificationProbe, OpenSummaryProbe, Probe  # noqa: E402
from viewlyt.live.server import LiveServer, apply_control, create_app  # noqa: E402
from viewlyt.live.window import WindowBuffer, WindowConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_persistence(tmp_path, monkeypatch):
    """Every state-changing control op persists: never touch the real ~/.viewlyt."""
    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class RecWS:
    """Dashboard-socket stand-in that records every frame the server broadcasts."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def frames(self, type_: str | None = None) -> list[dict]:
        out = [json.loads(s) for s in self.sent]
        return [f for f in out if type_ is None or f.get("type") == type_]


class RecRunner:
    """Fake LLMRunner: records (probe_id, n_messages) per call, can block/fail/cost."""

    model = "fake-model"
    avg_latency_ms = 42

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.seen: dict[str, list[str]] = {}
        self.gate: asyncio.Event | None = None
        self.fail_ids: set[str] = set()
        self.cost_per_call = 0.0
        self.total_tokens = 0
        self.total_cost = 0.0
        self.closed = 0

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        self.calls.append((probe.id, len(messages)))
        self.seen[probe.id] = [m.text for m in messages]
        if self.gate is not None:
            await self.gate.wait()
        if probe.id in self.fail_ids:
            raise RuntimeError(f"{probe.id} exploded")
        self.total_tokens += 10
        self.total_cost += self.cost_per_call
        return {"summary": f"summary from {probe.id}"}

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:
        return {}

    async def aclose(self) -> None:
        self.closed += 1


def _msgs(k: int) -> list[ChatMessage]:
    return [ChatMessage(author=f"u{i}", text=f"m{i}", ts=float(i)) for i in range(k)]


def _open(pid: str, *, label: str | None = None, interval_s: float = 0.0, sample_n: int = 0):
    p = OpenSummaryProbe(id=pid, label=pid if label is None else label, instruction="sum it")
    p.interval_s = interval_s
    p.sample_n = sample_n
    return p


def _server(**window) -> tuple[LiveServer, RecWS, RecRunner]:
    srv = LiveServer(LLMConfig(), WindowConfig(**window))
    runner = RecRunner()
    srv._client = runner  # type: ignore[assignment]
    ws = RecWS()
    srv.dash.active = {ws}  # type: ignore[assignment]
    return srv, ws, runner


async def _settle() -> None:
    """Await every other live task (the fire-and-forget rewrite/suggest/decompose ones)."""
    cur = asyncio.current_task()
    while True:
        others = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
        if not others:
            return
        await asyncio.gather(*others, return_exceptions=True)


def _saved_state() -> dict | None:
    return persistence.load_state()


# ---------------------------------------------------------------------------
# Worker harness: fake clock + fake queue so a "tick" costs ~1 ms, no real waiting
# ---------------------------------------------------------------------------


class _Clock:
    """Stands in for the ``time`` module inside ``live_server`` (monotonic + wall)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0


class _FakeQueue:
    """``get()`` yields queued items, then behaves like an empty queue timing out."""

    def __init__(self, items: list | None = None) -> None:
        self.items = deque(items or [])
        self.get_calls = 0

    async def get(self):
        self.get_calls += 1
        if self.items:
            return self.items.popleft()
        await asyncio.sleep(0.001)
        raise TimeoutError

    def put_nowait(self, item) -> None:
        self.items.append(item)


class Env:
    """A LiveServer wired to the fake clock/queue/runner, plus polling helpers."""

    def __init__(self, monkeypatch, **window) -> None:
        window.setdefault("n", 2)
        window.setdefault("mode", "count")
        self.clock = _Clock()
        monkeypatch.setattr(live_server, "time", self.clock)
        self.server, self.ws, self.runner = _server(**window)
        self.q = _FakeQueue()
        self.server.queue = self.q  # type: ignore[assignment]

    def add(self, *probes: Probe) -> None:
        for p in probes:
            self.server.probes[p.id] = p

    def feed(self, k: int) -> None:
        """Push ``k`` messages straight into the buffer (as if the worker had ingested them)."""
        base = self.server.ingested
        for i in range(k):
            self.server.buffer.add(ChatMessage(author=f"u{base + i}", text=f"m{base + i}", ts=1.0))
        self.server.ingested += k

    def runs(self) -> int:
        """Finished batches == ``proc(active=False)`` frames."""
        return sum(1 for f in self.ws.frames("proc") if not f["active"])

    def errors(self) -> list[str]:
        return [f["message"] for f in self.ws.frames("error")]

    async def until(self, pred, what: str = "condition") -> None:
        for _ in range(2000):  # bounded: ~2 s worst case, only ever hit on failure
            if pred():
                return
            await asyncio.sleep(0.001)
        raise AssertionError(f"timed out waiting for {what}")

    async def ticks(self, k: int = 6) -> None:
        """Let the worker loop iterate ``k`` more times (negative assertions need this)."""
        target = self.q.get_calls + k
        await self.until(lambda: self.q.get_calls >= target, "worker ticks")

    async def run(self, scenario) -> None:
        task = asyncio.create_task(live_server.worker(self.server))
        try:
            await scenario(self)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A failed assertion can leave a batch parked on the gate: release it, and bound
            # the drain so a red test fails fast instead of hanging the whole file.
            if self.runner.gate is not None:
                self.runner.gate.set()
            try:
                await asyncio.wait_for(_settle(), timeout=2.0)
            except TimeoutError:
                cur = asyncio.current_task()
                leftovers = [t for t in asyncio.all_tasks() if t is not cur and not t.done()]
                for t in leftovers:
                    t.cancel()
                await asyncio.gather(*leftovers, return_exceptions=True)


@pytest.fixture
def env(monkeypatch) -> Env:
    return Env(monkeypatch)


# ---------------------------------------------------------------------------
# Worker: scheduling
# ---------------------------------------------------------------------------


def test_worker_starts_paused_and_runs_once_resumed(env: Env) -> None:
    env.add(_open("a"))
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.ticks()
        assert e.runner.calls == [] and e.runs() == 0  # paused: zero LLM spend
        e.server.paused = False
        await e.until(lambda: e.runs() == 1, "first run")
        assert e.runner.calls == [("a", 2)]  # sample capped at window.n

    asyncio.run(env.run(scenario))


def test_worker_never_runs_with_empty_buffer_or_no_probes(env: Env) -> None:
    env.server.paused = False
    env.add(_open("a"))  # probes but no messages

    async def scenario(e: Env) -> None:
        await e.ticks()
        assert e.runner.calls == []
        e.server.probes.clear()
        e.feed(3)  # messages but no probes
        e.server.force_now = True
        await e.ticks()
        assert e.runner.calls == [] and e.runs() == 0

    asyncio.run(env.run(scenario))


def test_worker_per_probe_interval_scheduling_and_global_timer_isolation(monkeypatch) -> None:
    # A has its own 60 s cadence; B follows the global count policy (stride 100).
    env = Env(monkeypatch, n=100)
    a, b = _open("A", interval_s=60), _open("B")
    env.add(a, b)
    env.server.paused = False
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "first batch")
        assert sorted(c[0] for c in e.runner.calls) == ["A", "B"]  # never-run -> both due
        assert e.server.probe_runs == {"A": 1000.0, "B": 1000.0}

        # 10 s later, one new message: A's 60 s interval has not elapsed and the global
        # count cadence (100) has not fired for B -> nothing runs.
        e.clock.now = 1010.0
        e.feed(1)
        await e.ticks()
        assert e.runs() == 1

        # 61 s later A is due by its own clock; B still waits for the global policy.
        e.clock.now = 1061.0
        e.feed(1)
        await e.until(lambda: e.runs() == 2, "A-only batch")
        assert [c[0] for c in e.runner.calls[2:]] == ["A"]
        assert e.server.probe_runs == {"A": 1061.0, "B": 1000.0}

        # The A-only run must NOT have reset the global window counters: 5 messages
        # (3+1+1) were seen since the last global emit, so 95 more reach the stride of
        # 100 only if mark_emitted was skipped. Checked synchronously (no yield).
        e.feed(95)
        assert e.server.buffer.due(e.server.window, e.clock.now) is True
        await e.until(lambda: e.runs() == 3, "global cadence batch")
        assert [c[0] for c in e.runner.calls[3:]] == ["B"]

    asyncio.run(env.run(scenario))


def test_worker_global_cadence_resets_after_global_batch(monkeypatch) -> None:
    # Counterpart of the isolation test: when the GLOBAL cadence fires, the counters
    # reset, so the same stride is needed again before the next global batch.
    env = Env(monkeypatch, n=4)
    env.add(_open("B"))
    env.server.paused = False
    env.feed(4)  # == stride -> global_due

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "first batch")
        e.feed(1)
        assert e.server.buffer.due(e.server.window, e.clock.now) is False  # reset to 0 -> 1
        await e.ticks()
        assert e.runs() == 1
        e.feed(3)  # 4 since the emit -> due again
        await e.until(lambda: e.runs() == 2, "second batch")

    asyncio.run(env.run(scenario))


def test_worker_sizes_the_sample_for_the_largest_per_probe_sample_n(monkeypatch) -> None:
    env = Env(monkeypatch, n=2)
    env.add(_open("dflt"), _open("big", sample_n=5), _open("tiny", sample_n=1))
    env.server.paused = False
    env.feed(8)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "batch")
        seen = dict(e.runner.calls)
        # buffer.sample was taken with n=max(2, 5, 1)=5; each probe sees its own tail.
        assert seen == {"dflt": 2, "big": 5, "tiny": 1}
        # ...and the tail is the NEWEST messages (m7 is the latest), not the oldest.
        assert e.runner.seen == {
            "dflt": ["m6", "m7"],
            "big": ["m3", "m4", "m5", "m6", "m7"],
            "tiny": ["m7"],
        }

    asyncio.run(env.run(scenario))


def test_worker_idle_cost_guard_and_force_run_override(env: Env) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "first run")
        # No NEW messages: even with the global timer long expired, nothing is paid for.
        e.clock.now += 10_000
        await e.ticks()
        assert e.runs() == 1
        # A forced run bypasses the idle guard...
        await apply_control(e.server, {"op": "force_run"})
        await e.until(lambda: e.runs() == 2, "forced run")
        assert e.server.force_now is False  # ...and the intent is consumed once launched.
        await e.ticks()
        assert e.runs() == 2

    asyncio.run(env.run(scenario))


def test_worker_force_run_bypasses_pause(env: Env) -> None:
    env.add(_open("a"))
    env.feed(3)
    env.server.force_now = True
    assert env.server.paused is True

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "forced run while paused")
        assert e.server.paused is True

    asyncio.run(env.run(scenario))


def test_worker_force_intent_is_kept_until_there_is_something_to_run(env: Env) -> None:
    env.add(_open("a"))
    env.server.force_now = True  # empty buffer: cannot launch yet

    async def scenario(e: Env) -> None:
        await e.ticks()
        assert e.server.force_now is True and e.runs() == 0
        e.feed(3)
        await e.until(lambda: e.runs() == 1, "run once messages exist")
        assert e.server.force_now is False

    asyncio.run(env.run(scenario))


def test_worker_force_during_inflight_batch_runs_right_after(env: Env) -> None:
    # Regression: the "run immediately" intent must survive a tick where a batch is
    # already in flight (slow LLM), not be silently dropped.
    env.add(_open("a"))
    env.server.paused = False
    env.feed(3)
    gate = asyncio.Event()
    env.runner.gate = gate

    async def scenario(e: Env) -> None:
        await e.until(lambda: len(e.runner.calls) == 1, "batch in flight")
        assert e.server.processing is True
        e.server.force_now = True
        await e.ticks()
        assert e.server.force_now is True and len(e.runner.calls) == 1  # one batch at a time
        gate.set()
        await e.until(lambda: e.runs() == 2, "forced follow-up batch")
        assert e.server.force_now is False

    asyncio.run(env.run(scenario))


def test_worker_forced_run_runs_every_probe_even_if_not_individually_due(monkeypatch) -> None:
    env = Env(monkeypatch, n=100)
    env.add(_open("A", interval_s=3600), _open("B", interval_s=3600))
    env.server.paused = False
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "first batch")
        e.feed(1)
        await e.ticks()
        assert e.runs() == 1  # neither interval elapsed
        e.server.force_now = True
        await e.until(lambda: e.runs() == 2, "forced batch")
        assert sorted(c[0] for c in e.runner.calls[2:]) == ["A", "B"]

    asyncio.run(env.run(scenario))


# --- budget gate -----------------------------------------------------------


def test_worker_budget_cap_blocks_notifies_once_and_unlatches(env: Env) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.server.llm_cfg = LLMConfig(budget_usd=1.0)
    env.server.total_cost = 1.0  # exactly at the cap counts as reached
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.server.budget_blocked, "budget latch")
        await e.ticks()
        assert e.runner.calls == []
        msgs = [m for m in e.errors() if "Budget $1.00 reached" in m]
        assert len(msgs) == 1  # told ONCE, not spammed every tick

        e.server.force_now = True  # forced runs are capped too, still no extra frame
        await e.ticks()
        assert e.runner.calls == [] and len([m for m in e.errors() if "Budget" in m]) == 1
        assert e.server.force_now is True  # intent survives until budget allows it

        e.server.llm_cfg = LLMConfig(budget_usd=5.0)  # user raised the budget
        await e.until(lambda: e.runs() == 1, "run after raising the budget")
        assert e.server.budget_blocked is False
        assert e.server.force_now is False

    asyncio.run(env.run(scenario))


def test_worker_budget_relatches_after_a_new_breach(env: Env) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.server.llm_cfg = LLMConfig(budget_usd=1.0)
    env.server.total_cost = 1.0
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.server.budget_blocked, "first latch")
        e.server.total_cost = 0.0  # e.g. reset: under budget -> latch clears
        await e.until(lambda: e.runs() == 1, "run")
        assert e.server.budget_blocked is False
        e.server.total_cost = 2.0
        e.feed(2)
        await e.until(lambda: e.server.budget_blocked, "second latch")
        assert len([m for m in e.errors() if "Budget" in m]) == 2

    asyncio.run(env.run(scenario))


def test_worker_budget_zero_means_unlimited_and_idle_breach_is_silent(env: Env) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.server.total_cost = 999.0  # budget_usd == 0 -> no cap at all
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "uncapped run")
        assert e.server.budget_blocked is False and e.errors() == []

    asyncio.run(env.run(scenario))


def test_worker_over_budget_but_idle_is_silent(env: Env) -> None:
    # Over budget yet nothing wants to run (paused, not forced): no error, no latch.
    env.add(_open("a"))
    env.server.llm_cfg = LLMConfig(budget_usd=1.0)
    env.server.total_cost = 5.0
    env.feed(3)

    async def scenario(e: Env) -> None:
        await e.ticks()
        assert e.errors() == [] and e.server.budget_blocked is False

    asyncio.run(env.run(scenario))


# --- stop_batch / survival / feed -----------------------------------------


def test_stop_batch_cancels_inflight_and_clears_processing(env: Env) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.feed(3)
    env.runner.gate = asyncio.Event()  # never set: the batch hangs until cancelled

    async def scenario(e: Env) -> None:
        await e.until(lambda: len(e.runner.calls) == 1, "batch in flight")
        assert e.server.processing is True
        assert e.server.state_message()["analyzing_probe_ids"] == ["a"]
        task = e.server.current_batch_task
        await apply_control(e.server, {"op": "stop_batch"})
        assert task is not None
        await e.until(task.done, "cancelled batch finished its cleanup")
        assert task.cancelled() and e.server.processing is False
        assert e.server.analyzing_probe_ids == []
        assert e.runs() == 1  # the `finally` still told the dashboard to un-glow
        assert e.ws.frames("result") == []
        # The loop is alive: the next (forced) run completes normally.
        e.runner.gate = None
        await apply_control(e.server, {"op": "force_run"})
        await e.until(lambda: e.runs() == 2, "run after cancel")
        assert len(e.ws.frames("result")) == 1

    asyncio.run(env.run(scenario))


def test_worker_survives_an_iteration_failure(env: Env, caplog, monkeypatch) -> None:
    env.add(_open("a"))
    env.server.paused = False
    env.feed(3)
    real_due = WindowBuffer.due
    boom = {"n": 0}

    def flaky(self, cfg, now):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("transient failure")
        return real_due(self, cfg, now)

    monkeypatch.setattr(WindowBuffer, "due", flaky)

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.runs() == 1, "run after the failed iteration")

    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(env.run(scenario))
    assert "worker iteration failed" in caplog.text


def test_worker_mirrors_ingested_messages_to_chat_and_stat_frames(env: Env) -> None:
    env.q.items.extend(_msgs(3))

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.server.ingested == 3, "ingest")
        assert len(e.server.buffer) == 3
        e.clock.now += 1.0  # past the 0.25 s flush interval
        await e.until(
            lambda: (
                sum(len(f["items"]) for f in e.ws.frames("chat")) == 3
                and any(f["ingested"] == 3 for f in e.ws.frames("stat"))
            ),
            "chat + stat flush",
        )
        items = [i for f in e.ws.frames("chat") for i in f["items"]]
        assert [i["text"] for i in items] == ["m0", "m1", "m2"]
        assert items[0] == {"author": "u0", "text": "m0"}
        stat = [f for f in e.ws.frames("stat") if f["ingested"] == 3][-1]
        assert stat["buffer"] == 3

    asyncio.run(env.run(scenario))


def test_worker_caps_the_pending_feed_at_5000_items(env: Env) -> None:
    env.q.items.extend(ChatMessage(author=f"u{i}", text=f"m{i}", ts=1.0) for i in range(5002))

    async def scenario(e: Env) -> None:
        await e.until(lambda: e.server.ingested == 5002, "drain the queue")
        e.clock.now += 1.0
        await e.until(lambda: sum(len(f["items"]) for f in e.ws.frames("chat")) >= 5001, "flush")
        texts = [i["text"] for f in e.ws.frames("chat") for i in f["items"]]
        # m0 flushed alone on the first tick; of the 5001 pending, the oldest (m1) was shed.
        assert texts[0] == "m0" and texts[1] == "m2" and texts[-1] == "m5001"
        assert len(texts) == 5001

    asyncio.run(env.run(scenario))


# ---------------------------------------------------------------------------
# process_window / _run_window
# ---------------------------------------------------------------------------


def test_process_window_with_no_probes_is_a_noop() -> None:
    srv, ws, _ = _server()
    srv._client = None
    asyncio.run(live_server.process_window(srv, _msgs(3), 1.0))
    assert ws.sent == [] and srv._client is None  # not even a client was built


def test_process_window_total_failure_names_endpoint_and_model() -> None:
    srv, ws, runner = _server()
    srv.llm_cfg = LLMConfig(base_url="http://llm.invalid/v1", model="acme/model-x")
    srv.probes.update({"a": _open("a"), "b": _open("b")})
    runner.fail_ids = {"a", "b"}
    asyncio.run(live_server.process_window(srv, _msgs(3), 1.0))

    errs = ws.frames("error")
    assert len(errs) == 1  # one "all failed" frame, not one per probe
    assert "All probes failed" in errs[0]["message"]
    assert "http://llm.invalid/v1" in errs[0]["message"] and "acme/model-x" in errs[0]["message"]
    assert ws.frames("result") == [] and srv.history == {}
    assert len(ws.frames("cost")) == 1 and len(ws.frames("stat")) == 1  # still settles


def test_process_window_partial_failure_falls_back_to_id_when_label_empty() -> None:
    srv, ws, runner = _server()
    srv.probes.update({"ok": _open("ok"), "boom": _open("boom", label="")})
    runner.fail_ids = {"boom"}
    asyncio.run(live_server.process_window(srv, _msgs(3), 1.0))

    assert [f["probe_id"] for f in ws.frames("result")] == ["ok"]
    assert [f["message"] for f in ws.frames("error")] == [
        "Probe 'boom' failed this batch (see the server log)."
    ]


def test_process_window_cost_frames_report_delta_and_running_totals() -> None:
    srv, ws, runner = _server()
    runner.cost_per_call = 0.25
    srv.probes.update({"a": _open("a"), "b": _open("b")})
    asyncio.run(live_server.process_window(srv, _msgs(3), 5.0))
    asyncio.run(live_server.process_window(srv, _msgs(3), 6.0))

    c1, c2 = ws.frames("cost")
    assert (c1["tokens_delta"], c1["tokens_total"]) == (20, 20)
    assert (c1["cost_delta"], c1["cost_total"]) == (0.5, 0.5)
    assert (c2["tokens_delta"], c2["tokens_total"]) == (20, 40)
    assert (c2["cost_delta"], c2["cost_total"]) == (0.5, 1.0)
    assert srv.total_tokens == 40 and srv.total_cost == pytest.approx(1.0)
    assert [f["window"] for f in ws.frames("stat")] == [3, 3]


def test_process_window_stamps_results_and_bounds_the_history() -> None:
    srv, ws, _ = _server()
    srv.probes["a"] = _open("a")
    for i in range(live_server._HISTORY_CAP + 5):
        asyncio.run(live_server.process_window(srv, _msgs(2), float(i)))
    hist = srv.history["a"]
    assert len(hist) == live_server._HISTORY_CAP
    assert hist[0]["ts"] == 5.0 and hist[-1]["ts"] == float(live_server._HISTORY_CAP + 4)


def test_run_window_brackets_with_proc_frames_and_clears_the_guard() -> None:
    srv, ws, _ = _server()
    a = _open("a")
    srv.probes["a"] = a
    srv.processing = True
    asyncio.run(live_server._run_window(srv, _msgs(2), 1.0, [a]))

    procs = ws.frames("proc")
    assert procs[0] == {"type": "proc", "active": True, "probe_ids": ["a"]}
    end = procs[1]
    assert end["active"] is False and end["probe_ids"] == ["a"]
    assert isinstance(end["latency_ms"], int) and end["latency_ms"] >= 0
    assert end["avg_latency_ms"] == 42  # read from the already-built client
    assert srv.processing is False and srv.analyzing_probe_ids == []
    assert srv.avg_latency_ms == 42 and srv.last_latency_ms == end["latency_ms"]
    # Ordering: start frame, then results, then the closing frame.
    types = [f["type"] for f in ws.frames()]
    assert types.index("proc") < types.index("result") < len(types) - 1


def test_run_window_without_explicit_probes_reports_empty_probe_ids() -> None:
    srv, ws, _ = _server()
    srv.probes["a"] = _open("a")
    asyncio.run(live_server._run_window(srv, _msgs(2), 1.0))
    assert [f["probe_ids"] for f in ws.frames("proc")] == [[], []]
    assert len(ws.frames("result")) == 1  # probes=None -> every probe still ran


def test_run_window_still_closes_the_batch_when_the_client_cannot_be_built(monkeypatch) -> None:
    # A client-construction failure must neither freeze the dashboard on "analyzing…"
    # nor be retried inside the `finally` (avg latency simply stays unknown).
    def explode(cfg):
        raise RuntimeError("cannot build runner")

    monkeypatch.setattr(live_server, "build_runner", explode)
    srv = LiveServer(LLMConfig(), WindowConfig())
    ws = RecWS()
    srv.dash.active = {ws}  # type: ignore[assignment]
    a = _open("a")
    srv.probes["a"] = a
    srv.processing = True
    with pytest.raises(RuntimeError, match="cannot build runner"):
        asyncio.run(live_server._run_window(srv, _msgs(2), 1.0, [a]))
    assert srv.processing is False and srv.avg_latency_ms is None
    assert ws.frames("proc")[-1]["active"] is False


# ---------------------------------------------------------------------------
# apply_control: every op
# ---------------------------------------------------------------------------


def test_transient_ops_flip_flags_broadcast_state_and_do_not_persist() -> None:
    srv, ws, _ = _server()

    async def go() -> None:
        await apply_control(srv, {"op": "resume"})
        assert srv.paused is False
        await apply_control(srv, {"op": "pause"})
        assert srv.paused is True
        await apply_control(srv, {"op": "force_run"})
        assert srv.force_now is True

    asyncio.run(go())
    states = ws.frames("state")
    assert [s["paused"] for s in states] == [False, True, True]  # a state frame per op
    assert not persistence.STATE_FILE.exists()  # nothing transient hits the disk


def test_clear_empties_the_buffer_but_not_the_counters() -> None:
    srv, ws, _ = _server()
    for m in _msgs(5):
        srv.buffer.add(m)
    srv.ingested = 5
    asyncio.run(apply_control(srv, {"op": "clear"}))
    assert len(srv.buffer) == 0 and srv.ingested == 5
    assert not persistence.STATE_FILE.exists()
    assert ws.frames("state")[-1]["ingested"] == 5


def test_clear_buffer_keeps_the_configured_capacity() -> None:
    # Regression for a REAL BUG (server.py `op == "clear"`): the fresh buffer is
    # `WindowBuffer()` -> default maxlen 4000, ignoring `server.window.capacity` that every
    # other reset path (set_window / reset_state / run) honors. Fails until fixed.
    srv, _, _ = _server(capacity=100)
    srv.buffer = WindowBuffer(maxlen=srv.window.capacity)
    asyncio.run(apply_control(srv, {"op": "clear"}))
    for m in _msgs(150):
        srv.buffer.add(m)
    assert len(srv.buffer) == 100


def test_upsert_probe_new_forces_a_run_existing_does_not_and_both_persist() -> None:
    srv, ws, _ = _server()
    new = {"kind": "open", "id": "o1", "label": "First", "instruction": "x"}
    asyncio.run(apply_control(srv, {"op": "upsert_probe", "probe": new}))
    assert srv.force_now is True and srv.probes["o1"].label == "First"
    st = _saved_state()
    assert st is not None and [p["id"] for p in st["probes"]] == ["o1"]

    srv.force_now = False
    edited = {**new, "label": "Renamed", "interval_s": 30, "sample_n": 7}
    asyncio.run(apply_control(srv, {"op": "upsert_probe", "probe": edited}))
    assert srv.force_now is False  # an edit is not a "run me now"
    assert srv.probes["o1"].label == "Renamed"
    assert (srv.probes["o1"].interval_s, srv.probes["o1"].sample_n) == (30.0, 7)
    assert [p["id"] for p in ws.frames("state")[-1]["probes"]] == ["o1"]


def test_malformed_op_is_swallowed_and_state_is_still_broadcast(caplog) -> None:
    srv, ws, _ = _server()
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(
            apply_control(srv, {"op": "upsert_probe", "probe": {"kind": "nope", "id": "x"}})
        )
        asyncio.run(apply_control(srv, {"op": "upsert_probe"}))  # no "probe" key at all
        asyncio.run(apply_control(srv, {"op": "who_knows"}))  # unknown op
        asyncio.run(apply_control(srv, {}))  # no op
    assert srv.probes == {}
    assert len(ws.frames("state")) == 4
    assert "control op failed" in caplog.text


def test_non_object_control_frame_is_swallowed_and_state_still_broadcast() -> None:
    # Regression for a REAL BUG (server.py apply_control): `op = data.get("op")` is INSIDE
    # the try, so a JSON frame that is not an object (list / string / null) hits the
    # except, and the trailing `if op in {...}` reads the never-assigned `op`:
    # UnboundLocalError escapes and tears the /control socket down, although the docstring
    # promises a bad frame is swallowed and the fresh state frame always goes out.
    srv, ws, _ = _server()
    for k, junk in enumerate(([1, 2], "oops", None, 7), start=1):
        asyncio.run(apply_control(srv, junk))  # type: ignore[arg-type]
        assert len(ws.frames("state")) == k


def test_remove_probe_drops_probe_history_and_run_stamp() -> None:
    srv, _, _ = _server()
    srv.probes["a"] = _open("a")
    srv.history["a"] = deque([{"type": "result"}])
    srv.probe_runs["a"] = 5.0
    asyncio.run(apply_control(srv, {"op": "remove_probe", "id": "ghost"}))  # unknown id: no-op
    asyncio.run(apply_control(srv, {"op": "remove_probe"}))  # missing id: no-op
    assert "a" in srv.probes
    asyncio.run(apply_control(srv, {"op": "remove_probe", "id": "a"}))
    assert (srv.probes, srv.history, srv.probe_runs) == ({}, {}, {})
    st = _saved_state()
    assert st is not None and st["probes"] == []


def test_set_window_merges_known_keys_only_and_persists() -> None:
    srv, _, _ = _server()
    marker = srv.buffer
    asyncio.run(
        apply_control(srv, {"op": "set_window", "n": 50, "gap": 10, "mode": "time", "foo": 1})
    )
    d = srv.window.to_dict()
    assert (d["n"], d["gap"], d["mode"]) == (50, 10.0, "time")
    assert d["capacity"] == 3000 and d["dedupe"] is True  # untouched keys keep their value
    assert not hasattr(srv.window, "foo")
    assert srv.buffer is marker  # capacity unchanged -> the rolling buffer is kept
    st = _saved_state()
    assert st is not None and st["window"]["n"] == 50 and st["window"]["mode"] == "time"


def test_set_window_capacity_change_replaces_the_buffer() -> None:
    srv, _, _ = _server()
    for m in _msgs(5):
        srv.buffer.add(m)
    asyncio.run(apply_control(srv, {"op": "set_window", "capacity": 150}))
    assert srv.window.capacity == 150
    assert len(srv.buffer) == 0  # documented data-loss semantics of a resize
    for m in _msgs(200):
        srv.buffer.add(m)
    assert len(srv.buffer) == 150  # the new maxlen is enforced

    asyncio.run(apply_control(srv, {"op": "set_window", "capacity": 5}))
    assert srv.window.capacity == 100  # WindowConfig clamps to its floor


def test_set_model_updates_fields_persists_and_retires_the_client() -> None:
    srv, ws, runner = _server()
    srv.llm_cfg = LLMConfig(api_key="old-key", model="m/old", language="English")

    async def go() -> None:
        await apply_control(
            srv,
            {"op": "set_model", "model": "m/new", "budget": "2.5", "language": "", "api_key": "k2"},
        )
        await _settle()

    asyncio.run(go())
    assert (srv.llm_cfg.model, srv.llm_cfg.budget_usd, srv.llm_cfg.api_key) == ("m/new", 2.5, "k2")
    assert srv.llm_cfg.language == "English"  # empty string never wipes the language
    assert srv._client is None and runner.closed == 1  # old runner closed in the background
    st = _saved_state()
    assert st is not None and st["model"]["model"] == "m/new" and st["model"]["api_key"] == "k2"
    assert "k2" not in json.dumps(ws.frames("state")[-1])  # the key never reaches the UI


def test_set_model_never_forwards_the_stored_key_to_an_unrecognized_base_url() -> None:
    srv, _, _ = _server()
    srv.llm_cfg = LLMConfig(api_key="stored-key", base_url="https://openrouter.ai/api/v1")
    asyncio.run(apply_control(srv, {"op": "set_model", "base_url": "https://attacker.test/v1"}))
    assert srv.llm_cfg.base_url == "https://attacker.test/v1" and srv.llm_cfg.api_key == ""
    st = _saved_state()
    assert st is not None and st["model"]["api_key"] == ""
    # An explicit key for the new host is honored; a recognized provider keeps the stored one.
    asyncio.run(
        apply_control(srv, {"op": "set_model", "base_url": "https://b.test/v1", "api_key": "k"})
    )
    assert srv.llm_cfg.api_key == "k"
    srv.llm_cfg = LLMConfig(api_key="stored-key", base_url="https://b.test/v1")
    asyncio.run(apply_control(srv, {"op": "set_model", "base_url": "https://api.openai.com/v1"}))
    assert srv.llm_cfg.api_key == "stored-key"


def test_set_model_failure_redacts_the_api_key_in_the_log(caplog) -> None:
    srv, ws, _ = _server()
    before = srv.llm_cfg
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(
            apply_control(
                srv, {"op": "set_model", "api_key": "sk-TOPSECRET", "budget": "not-a-number"}
            )
        )
    assert srv.llm_cfg is before  # failed op changed nothing
    assert "sk-TOPSECRET" not in caplog.text
    assert "'api_key': '***'" in caplog.text
    assert len(ws.frames("state")) == 1


def test_stop_batch_without_or_after_a_batch_is_harmless() -> None:
    srv, ws, _ = _server()

    async def go() -> None:
        await apply_control(srv, {"op": "stop_batch"})  # nothing in flight
        done = asyncio.create_task(asyncio.sleep(0))
        await done
        srv.current_batch_task = done  # an already-finished task: cancel() is a no-op
        await apply_control(srv, {"op": "stop_batch"})
        assert not done.cancelled()

    asyncio.run(go())
    assert len(ws.frames("state")) == 2


def test_reset_state_wipes_runtime_and_disk_and_retires_client(tmp_path) -> None:
    srv, ws, runner = _server()
    asyncio.run(
        apply_control(
            srv,
            {"op": "upsert_probe", "probe": {"kind": "open", "id": "o", "instruction": "x"}},
        )
    )
    assert persistence.STATE_FILE.exists()
    srv.window = WindowConfig(n=9)
    srv.llm_cfg = LLMConfig(model="custom", budget_usd=3.0)
    srv.history["o"] = deque([{"x": 1}])
    srv.probe_runs["o"] = 1.0
    srv.total_tokens, srv.total_cost, srv.budget_blocked = 99, 9.9, True

    async def go() -> None:
        await apply_control(srv, {"op": "reset_state"})
        await _settle()

    asyncio.run(go())
    assert (srv.probes, srv.history, srv.probe_runs) == ({}, {}, {})
    assert srv.window == WindowConfig() and srv.llm_cfg == LLMConfig()
    assert (srv.total_tokens, srv.total_cost, srv.budget_blocked) == (0, 0.0, False)
    assert not persistence.STATE_FILE.exists()  # the saved state really is gone
    assert runner.closed == 1 and srv._client is None
    last = ws.frames("state")[-1]
    assert last["probes"] == [] and last["cost_total"] == 0


def test_reset_state_survives_an_unlink_failure(monkeypatch) -> None:
    class Stuck:
        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError("read-only fs")

    monkeypatch.setattr(persistence, "STATE_FILE", Stuck())
    srv, ws, _ = _server()
    srv.total_cost = 4.0
    asyncio.run(apply_control(srv, {"op": "reset_state"}))
    assert srv.total_cost == 0.0 and len(ws.frames("state")) == 1


# --- rewrite_probe -----------------------------------------------------------


def test_rewrite_probe_op_returns_immediately_and_the_task_adds_the_probe(monkeypatch) -> None:
    gate_holder: dict = {}

    async def fake_rewrite(client, kind, text, categories):
        await gate_holder["gate"].wait()
        gate_holder["args"] = (kind, text, categories)
        return {"instruction": "Across all messages, hype?", "label": "Hype meter", "max_words": 30}

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, ws, _ = _server()

    async def go() -> None:
        gate_holder["gate"] = asyncio.Event()
        await apply_control(
            srv, {"op": "rewrite_probe", "kind": "open", "text": "is chat hyped?", "categories": []}
        )
        # Non-blocking: the op already answered with the PRE-rewrite state.
        assert srv.probes == {} and ws.frames("state")[-1]["probes"] == []
        gate_holder["gate"].set()
        await _settle()

    asyncio.run(go())
    assert gate_holder["args"] == ("open", "is chat hyped?", [])
    p = srv.probes["hype-meter"]
    assert p.kind == "open" and p.label == "Hype meter" and p.instruction.startswith("Across")  # type: ignore[attr-defined]
    assert srv.force_now is True  # the new probe is analyzed right away
    st = _saved_state()
    assert st is not None and [x["id"] for x in st["probes"]] == ["hype-meter"]
    assert [x["id"] for x in ws.frames("state")[-1]["probes"]] == ["hype-meter"]  # post-rewrite


def test_rewrite_probe_defaults_kind_to_open_and_text_to_empty(monkeypatch) -> None:
    seen: dict = {}

    async def fake_rewrite(client, kind, text, categories):
        seen["args"] = (kind, text, categories)
        return {"instruction": "i", "label": "L"}

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, _, _ = _server()

    async def go() -> None:
        await apply_control(srv, {"op": "rewrite_probe"})
        await _settle()

    asyncio.run(go())
    assert seen["args"] == ("open", "", [])


def test_rewrite_auto_honors_the_kind_the_model_chose(monkeypatch) -> None:
    async def fake_rewrite(client, kind, text, categories):
        assert kind == "auto"
        return {
            "kind": "classification",
            "label": "Mood",
            "question": "How does this message feel?",
            "categories": ["happy", "angry"],
        }

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, _, _ = _server()

    async def go() -> None:
        await apply_control(srv, {"op": "rewrite_probe", "kind": "auto", "text": "mood?"})
        await _settle()

    asyncio.run(go())
    p = srv.probes["mood"]
    assert isinstance(p, ClassificationProbe) and p.categories == ["happy", "angry"]


def test_rewrite_classification_without_spec_categories_uses_the_callers(monkeypatch) -> None:
    async def fake_rewrite(client, kind, text, categories):
        return {"label": "Sentiment", "question": "Sentiment?", "categories": []}

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, _, _ = _server()

    async def go() -> None:
        await apply_control(
            srv,
            {
                "op": "rewrite_probe",
                "kind": "classification",
                "text": "sentiment",
                "categories": ["pos", "neg"],
            },
        )
        await _settle()

    asyncio.run(go())
    assert srv.probes["sentiment"].categories == ["pos", "neg"]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("kind", "categories", "expected_kind"),
    [("open", [], "open"), ("classification", ["a", "b"], "classification")],
)
def test_rewrite_failure_adds_the_raw_text_and_warns(
    monkeypatch, caplog, kind, categories, expected_kind
) -> None:
    async def fake_rewrite(client, kind, text, categories):
        raise RuntimeError("llm unreachable")

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, ws, _ = _server()

    async def go() -> None:
        await apply_control(
            srv,
            {
                "op": "rewrite_probe",
                "kind": kind,
                "text": "Why is chat mad?",
                "categories": categories,
            },
        )
        await _settle()

    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(go())
    p = srv.probes["why-is-chat-mad"]
    assert p.kind == expected_kind
    if expected_kind == "open":
        assert p.instruction == "Why is chat mad?"  # type: ignore[attr-defined]
    else:
        assert p.question == "Why is chat mad?" and p.categories == categories  # type: ignore[attr-defined]
    assert srv.force_now is True
    assert ws.frames("error")[0]["message"] == "Probe rewrite failed; added your text as-is."
    assert "probe rewrite failed" in caplog.text
    st = _saved_state()
    assert st is not None and [x["id"] for x in st["probes"]] == ["why-is-chat-mad"]


def test_rewrite_failure_with_an_existing_id_does_not_overwrite_it(monkeypatch) -> None:
    async def fake_rewrite(client, kind, text, categories):
        raise RuntimeError("down")

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    srv, _, _ = _server()
    keep = _open("hype", label="Original")
    srv.probes["hype"] = keep

    async def go() -> None:
        await apply_control(srv, {"op": "rewrite_probe", "kind": "open", "text": "Hype"})
        await _settle()

    asyncio.run(go())
    assert srv.probes["hype"] is keep  # collision bumped instead of clobbering the user's probe
    assert "hype-2" in srv.probes


def test_rewrite_task_never_raises_even_if_persisting_fails(monkeypatch, caplog) -> None:
    async def fake_rewrite(client, kind, text, categories):
        return {"instruction": "i", "label": "L"}

    async def bad_persist(server):
        raise OSError("disk full")

    monkeypatch.setattr(live_server, "rewrite_probe_spec", fake_rewrite)
    monkeypatch.setattr(live_server, "persist", bad_persist)
    srv, _, _ = _server()

    async def go() -> None:
        await live_server._rewrite_and_add(srv, "open", "txt", [])

    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(go())  # must not raise
    assert "rewrite task failed entirely" in caplog.text


@pytest.mark.parametrize(
    ("label", "existing", "expected"),
    [
        ("Hype Meter!", [], "hype-meter"),
        ("  --Weird__Label--  ", [], "weird-label"),
        ("Ação", [], "a-o"),  # non-[a-z0-9] runs collapse to a single dash
        ("", [], "rewrite-1"),  # empty slug falls back to rewrite-<count+1>
        ("???", ["x", "y"], "rewrite-3"),
        ("hype", ["hype"], "hype-2"),
        ("hype", ["hype", "hype-2", "hype-3"], "hype-4"),
    ],
)
def test_slugify_probe_id(label, existing, expected) -> None:
    srv, _, _ = _server()
    for pid in existing:
        srv.probes[pid] = _open(pid)
    assert live_server._slugify_probe_id(srv, label) == expected


# --- suggest / decompose -----------------------------------------------------


def _spec(label: str) -> dict:
    return {"kind": "open", "label": label, "instruction": "i", "max_words": 40}


def test_suggest_probes_op_broadcasts_unique_ids_and_does_not_persist(monkeypatch) -> None:
    got: dict = {}

    async def fake_suggest(client, text, messages):
        got["args"] = (text, [m.text for m in messages])
        return [_spec("Mood"), _spec("Mood")]  # same label twice

    monkeypatch.setattr(live_server, "suggest_probes", fake_suggest)
    srv, ws, _ = _server(n=2)
    for m in _msgs(4):
        srv.buffer.add(m)

    async def go() -> None:
        await apply_control(srv, {"op": "suggest_probes", "text": "what now?"})
        await _settle()

    asyncio.run(go())
    assert got["args"] == ("what now?", ["m2", "m3"])  # tail of the buffer, window.n long
    sugg = ws.frames("suggestions")
    assert len(sugg) == 1
    assert [p["id"] for p in sugg[0]["probes"]] == ["mood", "mood-2"]
    assert srv.probes == {}  # proposals only
    assert not persistence.STATE_FILE.exists()


def test_suggest_ids_avoid_existing_probes(monkeypatch) -> None:
    async def fake_suggest(client, text, messages):
        return [_spec("Mood"), _spec("Mood"), _spec("Mood")]

    monkeypatch.setattr(live_server, "suggest_probes", fake_suggest)
    srv, ws, _ = _server()
    srv.probes["mood"] = _open("mood")

    asyncio.run(live_server._suggest(srv, ""))
    ids = [p["id"] for p in ws.frames("suggestions")[0]["probes"]]
    assert len(set(ids)) == 3 and "mood" not in ids


@pytest.mark.parametrize("outcome", ["empty", "raises"])
def test_suggest_failure_broadcasts_an_error_frame(monkeypatch, caplog, outcome) -> None:
    async def fake_suggest(client, text, messages):
        if outcome == "raises":
            raise RuntimeError("llm down")
        return []

    monkeypatch.setattr(live_server, "suggest_probes", fake_suggest)
    srv, ws, _ = _server()
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(live_server._suggest(srv, "x"))
    assert ws.frames("suggestions") == []
    assert [f["message"] for f in ws.frames("error")] == ["Could not suggest probes right now."]


def test_suggest_and_decompose_tasks_never_raise(monkeypatch, caplog) -> None:
    async def malformed(client, text, messages):
        return [None]  # a spec that is not a dict: post-processing blows up after the LLM call

    monkeypatch.setattr(live_server, "suggest_probes", malformed)
    monkeypatch.setattr(live_server, "decompose_probe", malformed)
    srv, ws, _ = _server()
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        asyncio.run(live_server._suggest(srv, "t"))  # must not raise out of the task
        asyncio.run(live_server._decompose(srv, "t"))
    assert "suggest task failed entirely" in caplog.text
    assert "decompose task failed entirely" in caplog.text
    assert ws.frames("suggestions") == [] and srv.probes == {}


def test_decompose_probe_op_broadcasts_chips_with_unique_ids(monkeypatch) -> None:
    got: dict = {}

    async def fake_decompose(client, text, messages):
        got["args"] = (text, len(messages))
        return [_spec("Tech"), _spec("Tech")]

    monkeypatch.setattr(live_server, "decompose_probe", fake_decompose)
    srv, ws, _ = _server(n=3)
    for m in _msgs(5):
        srv.buffer.add(m)

    async def go() -> None:
        await apply_control(srv, {"op": "decompose_probe", "text": "tech problems and why"})
        await _settle()

    asyncio.run(go())
    assert got["args"] == ("tech problems and why", 3)
    assert [p["id"] for p in ws.frames("suggestions")[0]["probes"]] == ["tech", "tech-2"]
    assert srv.probes == {}


@pytest.mark.parametrize("outcome", ["empty", "raises"])
def test_decompose_failure_error_text(monkeypatch, outcome) -> None:
    async def fake_decompose(client, text, messages):
        if outcome == "raises":
            raise RuntimeError("llm down")
        return []

    monkeypatch.setattr(live_server, "decompose_probe", fake_decompose)
    srv, ws, _ = _server()
    asyncio.run(live_server._decompose(srv, "x"))
    assert [f["message"] for f in ws.frames("error")] == ["Could not split that request right now."]


def test_decompose_ids_avoid_existing_probes(monkeypatch) -> None:
    async def fake_decompose(client, text, messages):
        return [_spec("Tech"), _spec("Tech"), _spec("Tech")]

    monkeypatch.setattr(live_server, "decompose_probe", fake_decompose)
    srv, ws, _ = _server()
    srv.probes["tech"] = _open("tech")
    asyncio.run(live_server._decompose(srv, "t"))
    ids = [p["id"] for p in ws.frames("suggestions")[0]["probes"]]
    assert len(set(ids)) == 3 and "tech" not in ids


# ---------------------------------------------------------------------------
# persist / LiveServer helpers
# ---------------------------------------------------------------------------


def test_persist_round_trips_window_model_and_probes() -> None:
    srv, _, _ = _server()
    srv.window = WindowConfig(n=77, mode="count")
    srv.llm_cfg = LLMConfig(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-secret-1",
        model="m/x",
        budget_usd=1.5,
        language="English",
        decider_model="d/y",
    )
    srv.probes["o"] = _open("o", interval_s=12)
    asyncio.run(live_server.persist(srv))

    raw = persistence.STATE_FILE.read_text(encoding="utf-8")
    assert "sk-secret-1" not in raw  # encrypted at rest
    st = _saved_state()
    assert st is not None
    assert st["window"]["n"] == 77 and st["window"]["mode"] == "count"
    assert st["model"]["api_key"] == "sk-secret-1" and st["model"]["decider_model"] == "d/y"
    assert (st["model"]["budget"], st["model"]["language"]) == (1.5, "English")
    assert st["probes"][0]["id"] == "o" and st["probes"][0]["interval_s"] == 12.0


def test_decider_state_off_inactive_and_hybrid_states() -> None:
    srv, _, _ = _server()
    assert srv.decider_state() == "off"
    srv.llm_cfg = LLMConfig(base_url="http://localhost:1234/v1", decider_model="d")
    assert srv.decider_state() == "inactive"  # provider has no decisions endpoint
    srv.llm_cfg = LLMConfig(decider_model="d")  # OpenRouter
    assert srv.decider_state() == "ok"  # configured, no hybrid runner built yet
    assert srv._client is not None and srv.decider_state() == "ok"  # non-hybrid runner

    class _Chat:
        model = "m"

        async def run(self, probe, messages):
            return {"counts": {}}

    class _BrokenDecider:
        model = "d"

        async def classify(self, probe, messages):
            raise RuntimeError("decider down")

    cls_probe = ClassificationProbe("c", "C", "q?", ["a", "b"])
    clock = {"t": 0.0}
    hybrid = HybridRunner(
        _Chat(),  # type: ignore[arg-type]
        _BrokenDecider(),  # type: ignore[arg-type]
        clock=lambda: clock["t"],
    )
    srv._client = hybrid
    assert srv.decider_state() == "ok"

    async def fail_once(n: int) -> None:
        for _ in range(n):
            await hybrid.run(cls_probe, _msgs(2))

    asyncio.run(fail_once(1))
    assert srv.decider_state() == "degraded"
    asyncio.run(fail_once(2))
    assert srv.decider_state() == "open"
    assert srv.state_message()["decider_state"] == "open"


def test_describe_decider_lines() -> None:
    assert live_server.describe_decider(LLMConfig()) == "decider:      off"
    inactive = live_server.describe_decider(
        LLMConfig(base_url="http://localhost:1234/v1", decider_model="d/m")
    )
    assert "d/m INACTIVE" in inactive and "http://localhost:1234/v1" in inactive
    active = live_server.describe_decider(LLMConfig(decider_model="d/m"))
    assert active == "decider:      d/m (classification)"


def test_client_is_built_lazily_once_and_rebuilt_after_retire(monkeypatch) -> None:
    built: list[LLMConfig] = []

    def fake_build(cfg):
        built.append(cfg)
        return RecRunner()

    monkeypatch.setattr(live_server, "build_runner", fake_build)
    srv = LiveServer(LLMConfig(model="m/1"), WindowConfig())
    first = srv.client()
    assert srv.client() is first and len(built) == 1
    srv.retire_client()  # sync caller, no running loop: must not raise
    assert srv._client is None
    assert srv.client() is not first and len(built) == 2


def test_retire_client_waits_for_the_inflight_batch_then_closes() -> None:
    srv, _, runner = _server()

    async def go() -> None:
        release = asyncio.Event()

        async def batch() -> None:
            await release.wait()

        srv.current_batch_task = asyncio.create_task(batch())
        srv.retire_client()
        for _ in range(5):
            await asyncio.sleep(0)
        assert runner.closed == 0  # old runner still serves the in-flight batch
        release.set()
        await _settle()
        assert runner.closed == 1
        assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []

    asyncio.run(go())


def test_retire_client_swallows_close_errors_and_tolerates_runners_without_aclose() -> None:
    class BadCloser:
        async def aclose(self) -> None:
            raise OSError("already closed")

    srv = LiveServer(LLMConfig(), WindowConfig())

    async def go() -> None:
        cur = asyncio.current_task()
        srv._client = BadCloser()  # type: ignore[assignment]
        srv.retire_client()
        bg = [t for t in asyncio.all_tasks() if t is not cur]
        await _settle()
        # the close error is swallowed inside the bg task: it finishes cleanly, not failed
        assert srv._client is None
        assert all(t.done() and not t.cancelled() and t.exception() is None for t in bg)
        srv._client = SimpleNamespace()  # type: ignore[assignment]  # no aclose at all
        srv.retire_client()
        assert srv._client is None
        assert [t for t in asyncio.all_tasks() if t is not cur] == []  # nothing scheduled

    asyncio.run(go())


def test_retire_client_without_a_running_loop_just_drops_the_reference() -> None:
    srv, _, runner = _server()
    srv.retire_client()
    assert srv._client is None and runner.closed == 0  # nothing to await the close on


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------


def test_connection_manager_connect_accepts_and_disconnect_is_idempotent() -> None:
    mgr = live_server.ConnectionManager()
    ws = RecWS()
    asyncio.run(mgr.connect(ws))  # type: ignore[arg-type]
    assert ws.accepted and ws in mgr.active
    mgr.disconnect(ws)  # type: ignore[arg-type]
    mgr.disconnect(ws)  # type: ignore[arg-type]
    assert mgr.active == set()


def test_broadcast_with_no_sockets_skips_serialization(monkeypatch) -> None:
    dumps: list[object] = []
    real = live_server.json.dumps
    monkeypatch.setattr(live_server.json, "dumps", lambda o, *a, **k: dumps.append(o) or real(o))
    mgr = live_server.ConnectionManager()
    assert asyncio.run(mgr.broadcast({"type": "x"})) is None
    assert dumps == [] and mgr.active == set()  # nobody to send to -> nothing serialized
    ws = RecWS()
    mgr.active = {ws}  # type: ignore[assignment]
    asyncio.run(mgr.broadcast({"type": "x"}))
    assert len(dumps) == 1 and ws.frames() == [{"type": "x"}]  # control: with a socket it does


def test_broadcast_closes_a_failed_socket_quietly() -> None:
    class Broken(RecWS):
        async def send_text(self, payload: str) -> None:
            raise RuntimeError("gone")

    class BadClose(Broken):
        async def close(self) -> None:
            raise RuntimeError("close failed too")

    mgr = live_server.ConnectionManager()
    ok, broken, worse = RecWS(), Broken(), BadClose()
    mgr.active = {ok, broken, worse}  # type: ignore[assignment]

    async def go() -> None:
        await mgr.broadcast({"t": 1})
        await _settle()  # lets the best-effort close tasks run

    asyncio.run(go())
    assert mgr.active == {ok}
    assert broken.closed is True  # a clean close so the tab's auto-reconnect kicks in
    assert len(ok.sent) == 1


# ---------------------------------------------------------------------------
# Export content
# ---------------------------------------------------------------------------


def _history_server() -> LiveServer:
    srv, _, _ = _server()
    srv.window = WindowConfig(n=10)
    srv.llm_cfg = LLMConfig(api_key="sk-leak-check")
    srv.total_tokens, srv.total_cost, srv.ingested = 12, 0.1234567891, 5
    srv.probes["mood"] = ClassificationProbe(
        id="mood", label="Mood", question="q", categories=["happy", "sad"]
    )
    srv.probes["sum"] = OpenSummaryProbe(id="sum", label="Summary", instruction="i")
    srv.probes["idle"] = OpenSummaryProbe(id="idle", label="Idle", instruction="i")
    srv.history["mood"] = deque(
        [
            {
                "type": "result",
                "kind": "classification",
                "label": "Mood, live",
                "n": 4,
                "ts": 1_700_000_000.0,
                "pct": {"happy": 75.0, "sad": 25.0},
            }
        ]
    )
    srv.history["sum"] = deque(
        [
            {
                "type": "result",
                "label": "Summary",
                "n": 3,
                "ts": 1_700_000_100.0,
                "text": 'He said "hi", ok\nline2',
            },
            {"type": "result", "text": "no timestamp or kind here"},
        ]
    )
    return srv


def test_export_payload_content() -> None:
    srv = _history_server()
    payload = live_server.export_payload(srv)
    assert payload["totals"] == {"ingested": 5, "tokens": 12, "cost_usd": 0.123457}
    assert payload["window"]["n"] == 10
    assert "api_key" not in payload["model"] and "sk-leak-check" not in json.dumps(payload)
    by_id = {p["probe"]["id"]: p for p in payload["probes"]}
    assert list(by_id) == ["mood", "sum", "idle"]
    assert len(by_id["mood"]["history"]) == 1 and by_id["idle"]["history"] == []
    from datetime import datetime

    assert datetime.fromisoformat(payload["exported_at"]).tzinfo is not None


def test_export_csv_rows_escape_text_and_default_missing_fields() -> None:
    rows = list(csv.reader(io.StringIO(live_server.export_csv(_history_server()))))
    assert rows[0] == ["ts_utc", "probe_id", "kind", "label", "n", "category", "pct", "text"]
    body = rows[1:]
    assert len(body) == 2 + 2  # 2 categories + 2 open snapshots; "idle" has no rows
    assert body[0] == [
        "2023-11-14T22:13:20+00:00",
        "mood",
        "classification",
        "Mood, live",
        "4",
        "happy",
        "75.0",
        "",
    ]
    assert body[1][5:7] == ["sad", "25.0"]
    # Commas, quotes and newlines survive the CSV round trip in the text column.
    assert body[2][7] == 'He said "hi", ok\nline2' and body[2][2] == "open"
    # Missing ts -> epoch, missing kind -> the probe's kind, missing n -> 0, label -> "".
    assert body[3][0] == "1970-01-01T00:00:00+00:00"
    assert body[3][2:5] == ["open", "", "0"]
    assert not any(r[1] == "idle" for r in body)


# ---------------------------------------------------------------------------
# Origin / Host guards (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        (None, False),
        ("", False),
        ("https://youtube.com", True),
        ("https://www.youtube.com", True),
        ("https://studio.YouTube.com", True),
        ("http://www.youtube.com", False),
        ("https://evilyoutube.com", False),
        ("https://youtube.com.evil.test", False),
        ("https://[bad", False),  # urlsplit raises ValueError -> False, not a crash
    ],
)
def test_is_youtube_origin(origin, expected) -> None:
    assert live_server._is_youtube_origin(origin) is expected


@pytest.mark.parametrize(
    ("origin", "allow_yt", "expected"),
    [
        (None, False, True),  # non-browser client
        ("", False, True),
        ("http://127.0.0.1:8000", False, True),
        ("http://localhost:8000", False, True),
        ("http://LOCALHOST:8000", False, True),
        ("http://myhost:8000", False, True),  # equals the bound host
        ("http://localhost:9999", False, False),  # wrong port
        ("http://localhost", False, False),  # implicit :80
        ("https://localhost:8000", False, False),  # wrong scheme
        ("http://evil.test:8000", False, False),
        ("https://www.youtube.com", True, True),
        ("https://youtube.com", True, True),
        ("https://www.youtube.com", False, False),  # /dashboard & /control refuse YouTube
        ("https://evilyoutube.com", True, False),
        ("http://www.youtube.com", True, False),
        ("http://[bad", True, False),  # malformed URL
    ],
)
def test_origin_allowed(origin, allow_yt, expected) -> None:
    assert live_server._origin_allowed(origin, "myhost", 8000, allow_youtube=allow_yt) is expected


def test_origin_with_a_non_numeric_port_is_rejected_not_raised() -> None:
    # Regression for a REAL BUG (server.py _origin_allowed): `u.port` is read OUTSIDE the
    # try, so an Origin such as "http://localhost:abc" raises ValueError instead of
    # returning False. On a WebSocket handshake that surfaces as an unhandled server error
    # rather than a clean 1008 rejection. Fails until fixed.
    assert (
        live_server._origin_allowed("http://localhost:abc", "h", 8000, allow_youtube=False) is False
    )


@pytest.mark.parametrize(
    ("header", "allowed", "expected"),
    [
        ("anything.example", None, True),  # check disabled (wildcard insecure bind)
        (None, None, True),
        (None, live_server._LOOPBACK_HOSTS, False),  # missing Host is refused
        ("", live_server._LOOPBACK_HOSTS, False),
        ("127.0.0.1:8000", live_server._LOOPBACK_HOSTS, True),
        ("localhost", live_server._LOOPBACK_HOSTS, True),
        ("LOCALHOST:1", live_server._LOOPBACK_HOSTS, True),
        ("[::1]:8000", live_server._LOOPBACK_HOSTS, True),
        ("evil.test:8000", live_server._LOOPBACK_HOSTS, False),
        ("127.0.0.1.evil.test", live_server._LOOPBACK_HOSTS, False),
        ("[bad", live_server._LOOPBACK_HOSTS, False),  # unparsable -> refused
        ("myhost:80", live_server._LOOPBACK_HOSTS | {"myhost"}, True),
    ],
)
def test_host_allowed(header, allowed, expected) -> None:
    assert live_server._host_allowed(header, allowed) is expected


# ---------------------------------------------------------------------------
# HTTP routes + middleware
# ---------------------------------------------------------------------------


class _LoopbackClient(TestClient):
    """Websockets from the TestClient ignore ``base_url``: force a loopback Host."""

    def websocket_connect(self, url: str, *args, **kwargs):  # type: ignore[override]
        return super().websocket_connect("ws://127.0.0.1:8000" + url, *args, **kwargs)


def _app_client(srv: LiveServer | None = None, **kw) -> tuple[_LoopbackClient, LiveServer]:
    srv = srv or LiveServer(LLMConfig(), WindowConfig())
    return _LoopbackClient(create_app(srv, **kw), base_url="http://127.0.0.1:8000"), srv


def _wait(pred, what: str) -> None:
    for _ in range(1000):
        if pred():
            return
        time.sleep(0.002)
    raise AssertionError(f"timed out waiting for {what}")


def test_host_header_guard_returns_421_on_http_routes() -> None:
    client, _ = _app_client()
    assert client.get("/export.json").status_code == 200
    bad = client.get("/export.json", headers={"host": "evil.test"})
    assert bad.status_code == 421 and "Misdirected" in bad.text
    # Preflights are guarded too: a rebinding page can't even negotiate CORS.
    pre = client.options(
        "/ingest", headers={"host": "evil.test", "origin": "https://www.youtube.com"}
    )
    assert pre.status_code == 421


def test_allowed_hosts_none_disables_the_host_check() -> None:
    client, _ = _app_client(allowed_hosts=None)
    assert client.get("/export.json", headers={"host": "anything.example"}).status_code == 200


def test_cors_preflight_echoes_only_youtube_origins() -> None:
    client, _ = _app_client()
    yt = client.options("/ingest", headers={"origin": "https://www.youtube.com"})
    assert yt.status_code == 200
    assert yt.headers["access-control-allow-origin"] == "https://www.youtube.com"  # echo, never *
    assert yt.headers["access-control-allow-private-network"] == "true"
    assert yt.headers["access-control-allow-methods"] == "GET, POST, OPTIONS"
    assert yt.headers["vary"] == "Origin"

    for origin in ("https://evil.test", "https://evilyoutube.com", None):
        h = {"origin": origin} if origin else {}
        r = client.options("/ingest", headers=h)
        assert r.status_code == 200
        assert not any(k.startswith("access-control-") for k in r.headers)

    # Non-preflight responses to a YouTube origin carry the headers too; others don't.
    ok = client.get("/export.json", headers={"origin": "https://www.youtube.com"})
    assert ok.headers["access-control-allow-origin"] == "https://www.youtube.com"
    other = client.get("/export.json", headers={"origin": "https://evil.test"})
    assert "access-control-allow-origin" not in other.headers


def test_snippet_userscript_and_extension_routes_render_host_and_port() -> None:
    srv = LiveServer(LLMConfig(), WindowConfig())
    client = TestClient(create_app(srv), base_url="http://localhost:9123")

    js = client.get("/snippet.js")
    assert js.headers["content-type"].startswith("application/javascript")
    assert js.text == live_server.render_snippet("localhost", 9123)
    assert 'var PORT = "9123"' in js.text and 'var h = "localhost"' in js.text

    us = client.get("/viewlyt.user.js")
    assert us.text == live_server.render_userscript("localhost", 9123)
    assert us.text.startswith("// ==UserScript==")

    z = client.get("/viewlyt-extension.zip")
    assert z.headers["content-type"] == "application/zip"
    assert 'filename="viewlyt-extension.zip"' in z.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        assert sorted(zf.namelist()) == ["content.js", "manifest.json"]
        assert zf.read("content.js").decode() == live_server.render_snippet("localhost", 9123)


def test_routes_default_to_port_8000_when_the_url_has_none() -> None:
    client = TestClient(
        create_app(LiveServer(LLMConfig(), WindowConfig())), base_url="http://127.0.0.1"
    )
    assert 'var PORT = "8000"' in client.get("/snippet.js").text


def test_export_routes_serve_the_session_as_downloads() -> None:
    client, _ = _app_client(_history_server())
    j = client.get("/export.json")
    assert 'filename="viewlyt-live-export.json"' in j.headers["content-disposition"]
    body = j.json()
    assert [p["probe"]["id"] for p in body["probes"]] == ["mood", "sum", "idle"]
    c = client.get("/export.csv")
    assert c.headers["content-type"].startswith("text/csv")
    assert 'filename="viewlyt-live-export.csv"' in c.headers["content-disposition"]
    assert c.text.splitlines()[0].startswith("ts_utc,probe_id,kind")
    assert "Mood, live" in c.text


def test_index_serves_built_dashboard_or_a_helpful_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(live_server, "STATIC_DIR", tmp_path)  # nothing built here
    client, _ = _app_client()
    r = client.get("/")
    assert r.status_code == 200 and "hasn't been built" in r.text
    assert client.get("/assets/x.js").status_code == 404  # no /assets mount either

    (tmp_path / "index.html").write_text("<html>REAL DASH</html>", encoding="utf-8")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    client2, _ = _app_client()
    assert "REAL DASH" in client2.get("/").text
    assert client2.get("/assets/app.js").text == "console.log(1)"


# ---------------------------------------------------------------------------
# WebSocket routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/ingest", {"origin": "https://evil.test"}),
        ("/ingest", {"host": "evil.test"}),
        ("/dashboard", {"origin": "https://www.youtube.com"}),  # YouTube only for /ingest
        ("/dashboard", {"origin": "http://evil.test:8000"}),
        ("/control", {"origin": "https://www.youtube.com"}),
        ("/control", {"origin": "http://evil.test:8000"}),
        ("/control", {"host": "evil.test"}),
    ],
)
def test_websockets_reject_foreign_origin_or_host_with_1008(path, headers) -> None:
    client, srv = _app_client()
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect(path, headers=headers):
            pass
    assert ei.value.code == 1008
    assert srv.dash.active == set()


def test_ingest_accepts_youtube_origin_and_parses_batches_skipping_junk() -> None:
    client, srv = _app_client()
    with client.websocket_connect("/ingest", headers={"origin": "https://www.youtube.com"}) as ws:
        ws.send_text("{not json")  # malformed frame: skipped, bridge stays up
        ws.send_text("null")  # parses to [None] -> dropped
        ws.send_text(
            json.dumps(
                [
                    {"author": "alice", "html": "hello <b>world</b>"},
                    {"author": "ghost", "text": ""},  # empty after cleaning -> dropped
                    7,  # not an object -> dropped
                    {"author": "bob", "text": "yo", "ts": 1_700_000_000_000, "id": "x1"},
                ]
            )
        )
        ws.send_text(json.dumps({"author": "carol", "text": "single object payload"}))
        _wait(lambda: srv.queue.qsize() == 3, "ingested messages")  # socket still open
    got = [srv.queue.get_nowait() for _ in range(3)]
    assert [(m.author, m.text) for m in got] == [
        ("alice", "hello world"),
        ("bob", "yo"),
        ("carol", "single object payload"),
    ]
    assert got[1].ts == 1_700_000_000.0 and got[1].id == "x1"  # ms folded to seconds


def test_ingest_drops_silently_when_the_queue_is_full() -> None:
    client, srv = _app_client()
    srv.queue = asyncio.Queue(maxsize=2)
    batch = [{"author": f"u{i}", "text": f"m{i}"} for i in range(5)]
    with client.websocket_connect("/ingest") as ws:  # no Origin: a non-browser client
        ws.send_text(json.dumps(batch))
        _wait(lambda: srv.queue.qsize() == 2, "queue fill")
        assert srv.queue.get_nowait().text == "m0"  # newest (m2..m4) were shed, not queued
        # The socket survived the overflow: a later message is accepted normally.
        ws.send_text(json.dumps({"author": "z", "text": "still alive"}))
        _wait(lambda: srv.queue.qsize() == 2, "post-overflow message")
    assert [srv.queue.get_nowait().text for _ in range(2)] == ["m1", "still alive"]


def test_ingest_socket_error_is_logged_and_closes_cleanly(monkeypatch, caplog) -> None:
    def boom(items):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(live_server, "_parse_ingest_batch", boom)
    client, srv = _app_client()
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        with client.websocket_connect("/ingest") as ws:
            ws.send_text(json.dumps([{"text": "x"}]))
            _wait(lambda: "ingest socket error" in caplog.text, "error log")
    assert srv.queue.empty()


def test_dashboard_sends_state_first_then_history_backfill_only_when_present() -> None:
    client, srv = _app_client()
    with client.websocket_connect("/dashboard") as ws:
        state = ws.receive_json()
        assert state["type"] == "state" and state["paused"] is True
        assert "api_key" not in json.dumps(state)
        assert len(srv.dash.active) == 1
        ws.send_text("ping")  # keepalives are ignored
    _wait(lambda: srv.dash.active == set(), "dashboard disconnect")

    srv.history["a"] = deque([{"type": "result", "probe_id": "a", "ts": 1.0}])
    srv.history["empty"] = deque()
    with client.websocket_connect("/dashboard") as ws:
        assert ws.receive_json()["type"] == "state"
        hist = ws.receive_json()
        assert hist["type"] == "history"
        assert list(hist["probes"]) == ["a"]  # empty histories are not replayed
        assert hist["probes"]["a"][0]["probe_id"] == "a"


def test_control_ops_over_the_socket_reach_every_dashboard() -> None:
    client, srv = _app_client()
    with client:  # one shared event loop: dashboard and control sockets must share it
        with client.websocket_connect("/dashboard") as dash:
            assert dash.receive_json()["type"] == "state"
            with client.websocket_connect("/control") as ctl:
                ctl.send_json({"op": "resume"})
                assert _next_state(dash)["paused"] is False
                ctl.send_json({"op": "set_window", "n": 33})
                assert _next_state(dash)["window"]["n"] == 33
                ctl.send_json({"op": "pause"})
                assert _next_state(dash)["paused"] is True
    assert srv.window.n == 33


def _next_state(ws, limit: int = 40) -> dict:
    for _ in range(limit):
        msg = ws.receive_json()
        if msg.get("type") == "state":
            return msg
    raise AssertionError("no state frame")


def test_control_socket_closes_on_bad_json_and_applies_nothing_after() -> None:
    client, srv = _app_client()
    with client.websocket_connect("/control") as ctl:
        ctl.send_text("this is not json")
        ctl.send_json({"op": "resume"})  # arrives after the server dropped the socket
    assert srv.paused is True


def test_control_socket_survives_a_non_object_frame() -> None:
    # Same root cause as test_non_object_control_frame_is_swallowed_and_state_still_broadcast,
    # observed at the socket: a JSON list currently tears the control socket down, so the op
    # that follows is never applied. Correct behavior: the junk frame is ignored and the
    # next op IS applied. Fails until fixed.
    client, srv = _app_client()
    with client.websocket_connect("/control") as ctl:
        ctl.send_json([1, 2, 3])
        ctl.send_json({"op": "resume"})
        _wait(lambda: srv.paused is False, "resume applied after the junk frame")
    assert srv.paused is False


def test_lifespan_runs_the_worker_and_capture_start_stop() -> None:
    calls: list[str] = []

    class FakeCapture:
        def start(self) -> None:
            calls.append("start")

        def stop(self) -> None:
            calls.append("stop")

    srv = LiveServer(LLMConfig(), WindowConfig())
    client = _LoopbackClient(
        create_app(srv, capture=FakeCapture()), base_url="http://127.0.0.1:8000"
    )
    with client:
        assert calls == ["start"]
        with client.websocket_connect("/ingest") as ws:
            ws.send_text(json.dumps({"author": "a", "text": "hello"}))
        _wait(lambda: srv.ingested == 1, "worker consumed the message")  # worker is alive
        assert len(srv.buffer) == 1
    assert calls == ["start", "stop"]  # capture torn down off-loop on shutdown


# ---------------------------------------------------------------------------
# run(): restore / seed / guards with uvicorn stubbed
# ---------------------------------------------------------------------------


class _RunHarness:
    def __init__(self, monkeypatch) -> None:
        import uvicorn

        self.uvicorn_calls: list[dict] = []
        self.created: dict = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda app, **kw: self.uvicorn_calls.append({"app": app, **kw})
        )
        real_create = live_server.create_app

        def spy(server, capture=None, *, allowed_hosts=live_server._LOOPBACK_HOSTS):
            self.created.update(server=server, capture=capture, allowed_hosts=allowed_hosts)
            return real_create(server, capture=capture, allowed_hosts=allowed_hosts)

        monkeypatch.setattr(live_server, "create_app", spy)
        self.opened: list[str] = []
        monkeypatch.setattr(live_server.webbrowser, "open", lambda url: self.opened.append(url))

    @property
    def server(self) -> LiveServer:
        return self.created["server"]


@pytest.fixture
def harness(monkeypatch) -> _RunHarness:
    return _RunHarness(monkeypatch)


def test_run_refuses_a_non_loopback_bind_without_the_flag(harness) -> None:
    with pytest.raises(SystemExit) as ei:
        live_server.run(host="192.168.1.5")
    assert "no authentication" in str(ei.value).lower() and "--allow-insecure-bind" in str(ei.value)
    assert harness.uvicorn_calls == [] and not persistence.STATE_FILE.exists()


def test_run_fresh_start_seeds_the_state_file_and_serves(harness, capsys) -> None:
    cfg = LLMConfig(api_key="launch-key", model="m/fresh", budget_usd=2.0)
    live_server.run(host="127.0.0.1", port=8123, llm_cfg=cfg, decider_override="d/x")

    assert len(harness.uvicorn_calls) == 1
    call = harness.uvicorn_calls[0]
    assert (call["host"], call["port"], call["log_level"]) == ("127.0.0.1", 8123, "warning")
    assert hasattr(call["app"], "routes")
    assert harness.created["capture"] is None
    assert harness.created["allowed_hosts"] == live_server._LOOPBACK_HOSTS
    assert harness.opened == []

    st = _saved_state()
    assert st is not None  # seeded on first launch
    assert st["model"]["model"] == "m/fresh" and st["model"]["api_key"] == "launch-key"
    assert st["model"]["decider_model"] == "d/x" and st["model"]["budget"] == 2.0
    assert harness.server.llm_cfg.decider_model == "d/x"
    assert "d/x (classification)" in capsys.readouterr().out


def test_run_restores_saved_state_skipping_unrestorable_probes(harness, caplog) -> None:
    persistence.save_state(
        {"n": 77, "mode": "count", "capacity": 500},
        {
            "base_url": "http://localhost:1234/v1",
            "model": "m/saved",
            "api_key": "saved-key",
            "budget": 2.5,
            "language": "English",
            "decider_model": "d/saved",
        },
        [
            {"kind": "open", "id": "good", "label": "Good", "instruction": "x"},
            {"kind": "martian", "id": "bad"},  # unknown kind: must not abort startup
        ],
    )
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        live_server.run(llm_cfg=LLMConfig(api_key="launch-key"))
    srv = harness.server
    assert srv.window.n == 77 and srv.window.capacity == 500
    for m in _msgs(600):  # buffer sized from the RESTORED capacity: oldest evicted past 500
        srv.buffer.add(m)
    assert len(srv.buffer) == 500
    assert srv.llm_cfg.api_key == "launch-key"  # the launch key wins over the saved one
    assert (srv.llm_cfg.model, srv.llm_cfg.budget_usd) == ("m/saved", 2.5)
    assert srv.llm_cfg.language == "English" and srv.llm_cfg.decider_model == "d/saved"
    assert list(srv.probes) == ["good"]
    assert "skipping unrestorable saved probe" in caplog.text
    assert len(harness.uvicorn_calls) == 1


def test_run_decider_override_beats_saved_choice_and_is_persisted(harness) -> None:
    persistence.save_state(
        {},
        {"base_url": "https://openrouter.ai/api/v1", "model": "m", "decider_model": "d/saved"},
        [],
    )
    live_server.run(decider_override="")  # --no-decider
    assert harness.server.llm_cfg.decider_model == ""
    st = _saved_state()
    assert st is not None and st["model"]["decider_model"] == ""  # sticks for the next launch


def test_run_without_override_keeps_the_saved_decider(harness) -> None:
    persistence.save_state(
        {},
        {"base_url": "https://openrouter.ai/api/v1", "model": "m", "decider_model": "d/saved"},
        [],
    )
    live_server.run()
    assert harness.server.llm_cfg.decider_model == "d/saved"


def test_run_builds_the_capture_from_capture_url(harness, monkeypatch) -> None:
    from viewlyt.live import capture as cap_mod

    built: list[tuple] = []

    class FakeCapture:
        def __init__(self, url: str, snippet: str) -> None:
            built.append((url, snippet))

    monkeypatch.setattr(cap_mod, "BrowserCapture", FakeCapture)
    live_server.run(host="localhost", port=8456, capture_url="dQw4w9WgXcQ")
    assert len(built) == 1
    assert built[0][0] == cap_mod.popout_url("dQw4w9WgXcQ")
    assert built[0][1] == live_server.render_snippet("localhost", 8456)
    assert isinstance(harness.created["capture"], FakeCapture)


def test_run_open_browser_opens_the_dashboard_and_swallows_failures(harness, monkeypatch) -> None:
    live_server.run(host="127.0.0.1", port=8777, open_browser=True)
    assert harness.opened == ["http://127.0.0.1:8777/"]

    def broken(url):
        raise RuntimeError("no browser here")

    monkeypatch.setattr(live_server.webbrowser, "open", broken)
    live_server.run(open_browser=True)  # must still reach uvicorn.run
    assert len(harness.uvicorn_calls) == 2


def test_run_wildcard_insecure_bind_disables_the_host_pin_and_warns(harness, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        live_server.run(host="0.0.0.0", allow_insecure_bind=True)
    assert harness.created["allowed_hosts"] is None
    assert "allow-insecure-bind" in caplog.text
    assert harness.uvicorn_calls[0]["host"] == "0.0.0.0"


def test_run_named_insecure_bind_pins_loopback_plus_that_host(harness) -> None:
    live_server.run(host="LAN-Box", allow_insecure_bind=True)
    assert harness.created["allowed_hosts"] == live_server._LOOPBACK_HOSTS | {"lan-box"}


def test_per_probe_sample_n_sees_the_newest_messages_not_the_oldest() -> None:
    # A probe with its own sample_n analyses the TAIL of the shared window: the most
    # recent chat is what a live read is about. Slicing the head instead would silently
    # analyse stale messages while every count still looks right.
    srv, _ws, runner = _server(n=6)
    srv.probes = {"all": _open("all"), "tail": _open("tail", sample_n=2)}
    window = _msgs(6)
    asyncio.run(live_server.process_window(srv, window, 0.0))
    assert runner.seen["all"] == ["m0", "m1", "m2", "m3", "m4", "m5"]
    assert runner.seen["tail"] == ["m4", "m5"]
