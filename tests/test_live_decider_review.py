"""Regression tests for the decision-model review: silent zeros, hangs, circuit breaker,
host matching, cost estimate, attribution headers, token overhead, provider routing.

No network, no browser: ``httpx.MockTransport`` and fakes only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewlyt.live.decisions import (  # noqa: E402
    DEFAULT_DECIDER_MODEL,
    build_decision_requests,
    decisions_url,
    usage_from_response,
)
from viewlyt.live.messages import ChatMessage  # noqa: E402
from viewlyt.live.probes import ClassificationProbe  # noqa: E402


def _msgs(n: int) -> list[ChatMessage]:
    return [ChatMessage(author=f"u{i}", text=f"text number {i}", ts=float(i)) for i in range(n)]


def _probe() -> ClassificationProbe:
    return ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does the author feel?",
        categories=["happy", "sad", "neutral"],
    )


def _mock_client(handler, *, base_url="https://openrouter.ai/api/v1", model=None, **kw):
    pytest.importorskip("httpx")
    import httpx

    from viewlyt.live.decisions import DecisionClient

    return DecisionClient(
        base_url,
        "sk-test",
        model or DEFAULT_DECIDER_MODEL,
        transport=httpx.MockTransport(handler),
        **kw,
    )


def _ok_answers(body: dict, choice: str = "happy") -> dict:
    return {qid: {"type": "choice", "choice": choice} for qid in body["questions"]}


# --- item 2: silent zeros --------------------------------------------------------------


def test_classify_raises_on_a_200_without_usable_answers() -> None:
    httpx = pytest.importorskip("httpx")
    from viewlyt.live.decisions import DecisionResponseError

    for payload in ({}, {"answers": "nope"}, {"answers": {"m0": {"type": "number"}}}):
        client = _mock_client(lambda request, p=payload: httpx.Response(200, json=p))
        with pytest.raises(DecisionResponseError):
            asyncio.run(client.classify(_probe(), _msgs(3)))


def test_classify_tolerates_gaps_up_to_half_but_not_more() -> None:
    httpx = pytest.importorskip("httpx")
    from viewlyt.live.decisions import DecisionResponseError

    def answered(n_answered: int):
        def handler(request):
            body = json.loads(request.content)
            ids = list(body["questions"])[:n_answered]
            return httpx.Response(
                200, json={"answers": {q: {"type": "choice", "choice": "happy"} for q in ids}}
            )

        return handler

    out = asyncio.run(_mock_client(answered(2)).classify(_probe(), _msgs(4)))
    assert out["labels"] == ["happy", "happy", "", ""]  # exactly half: tolerated
    with pytest.raises(DecisionResponseError):
        asyncio.run(_mock_client(answered(1)).classify(_probe(), _msgs(4)))


class _FakeChat:
    model = "chat-model"

    def __init__(self) -> None:
        self.run_calls: list[str] = []
        self.total_tokens = 0
        self.total_cost = 0.0
        self._latency_count = 0
        self._latency_sum_ms = 0.0

    async def run(self, probe, messages) -> dict:
        self.run_calls.append(probe.id)
        return {"labels": ["chat"] * len(messages)}

    async def complete_json(self, system, user, schema) -> dict:
        return {}


def test_hybrid_falls_back_to_chat_on_an_empty_200() -> None:
    httpx = pytest.importorskip("httpx")
    from viewlyt.live.llm import HybridRunner

    client = _mock_client(lambda request: httpx.Response(200, json={"usage": {}}))
    runner = HybridRunner(_FakeChat(), client)  # type: ignore[arg-type]
    assert asyncio.run(runner.run(_probe(), _msgs(3))) == {"labels": ["chat"] * 3}


# --- item 3: hangs / concurrency / breaker / log ---------------------------------------


def test_classify_sends_batches_concurrently_bounded_and_in_order() -> None:
    httpx = pytest.importorskip("httpx")
    state = {"now": 0, "peak": 0}

    async def handler(request):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        body = json.loads(request.content)
        answers = {
            qid: {"type": "choice", "choice": "happy" if int(qid[1:]) % 2 == 0 else "sad"}
            for qid in body["questions"]
        }
        return httpx.Response(200, json={"answers": answers})

    client = _mock_client(handler)
    t0 = time.monotonic()
    out = asyncio.run(client.classify(_probe(), _msgs(230)))  # 8 batches
    elapsed = time.monotonic() - t0
    assert 1 < state["peak"] <= 4
    assert elapsed < 8 * 0.05 * 0.75  # well under the sequential 0.4 s
    assert out["labels"] == ["happy" if i % 2 == 0 else "sad" for i in range(230)]


def test_classify_failure_cancels_sibling_batches() -> None:
    httpx = pytest.importorskip("httpx")

    async def handler(request):
        body = json.loads(request.content)
        if "m0" in body["questions"]:
            return httpx.Response(500, json={})
        await asyncio.sleep(5)
        return httpx.Response(200, json={"answers": {}})

    client = _mock_client(handler)
    t0 = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.classify(_probe(), _msgs(96)))
    assert time.monotonic() - t0 < 2.0


def test_decider_has_its_own_short_timeout_independent_of_chat() -> None:
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    from viewlyt.live.decisions import DECIDER_TIMEOUT
    from viewlyt.live.llm import HybridRunner, LLMConfig, build_runner

    assert DECIDER_TIMEOUT <= 20.0
    r = build_runner(
        LLMConfig(
            base_url="https://openrouter.ai/api/v1",
            api_key="k",
            timeout=120.0,
            decider_model=DEFAULT_DECIDER_MODEL,
        )
    )
    assert isinstance(r, HybridRunner)
    assert r.decider.timeout == DECIDER_TIMEOUT
    assert r.chat.cfg.timeout == 120.0


def test_classify_gives_up_on_a_hung_request_at_the_total_deadline() -> None:
    httpx = pytest.importorskip("httpx")

    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    client = _mock_client(handler, timeout=0.1)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(client.classify(_probe(), _msgs(2)))
    assert time.monotonic() - t0 < 2.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _FlakyDecider:
    """Fails while ``fail`` is set, with an EMPTY-str error like httpx.ReadTimeout."""

    model = "decider-model"

    def __init__(self, fail: bool = True) -> None:
        self.fail = fail
        self.calls = 0
        self.total_tokens = 0
        self.total_cost = 0.0
        self._latency_count = 0
        self._latency_sum_ms = 0.0

    async def classify(self, probe, messages) -> dict:
        self.calls += 1
        if self.fail:
            raise TimeoutError()
        return {"labels": ["decider"] * len(messages)}


def _breaker():
    from viewlyt.live.llm import HybridRunner

    clock = _Clock()
    chat = _FakeChat()
    dec = _FlakyDecider()
    return HybridRunner(chat, dec, clock=clock), chat, dec, clock  # type: ignore[arg-type]


def test_breaker_opens_after_three_failures_then_skips_the_decider(caplog) -> None:
    runner, chat, dec, _ = _breaker()
    assert runner.decider_state == "ok"
    with caplog.at_level("INFO", logger="viewlyt.live"):
        for _ in range(3):
            assert asyncio.run(runner.run(_probe(), _msgs(2))) == {"labels": ["chat"] * 2}
        assert runner.decider_state == "open"
        for _ in range(4):  # open: straight to chat, decider untouched
            asyncio.run(runner.run(_probe(), _msgs(2)))
    assert dec.calls == 3 and len(chat.run_calls) == 7
    opened = [r for r in caplog.records if "skipping it" in r.getMessage()]
    assert len(opened) == 1 and opened[0].levelname == "WARNING"


def test_breaker_half_opens_after_cooldown_and_closes_on_success(caplog) -> None:
    runner, _, dec, clock = _breaker()
    for _ in range(3):
        asyncio.run(runner.run(_probe(), _msgs(1)))
    clock.t += 299
    assert runner.decider_state == "open"
    clock.t += 2  # 5 minutes elapsed
    dec.fail = False
    with caplog.at_level("INFO", logger="viewlyt.live"):
        out = asyncio.run(runner.run(_probe(), _msgs(1)))
        asyncio.run(runner.run(_probe(), _msgs(1)))
    assert out == {"labels": ["decider"]}
    assert dec.calls == 5 and runner.decider_state == "ok"
    closed = [r for r in caplog.records if "circuit closed" in r.getMessage()]
    assert len(closed) == 1 and closed[0].levelname == "INFO"


def test_breaker_reopens_on_a_single_failure_after_the_cooldown() -> None:
    runner, _, dec, clock = _breaker()
    for _ in range(3):
        asyncio.run(runner.run(_probe(), _msgs(1)))
    clock.t += 301
    asyncio.run(runner.run(_probe(), _msgs(1)))  # half-open probe, fails
    assert dec.calls == 4 and runner.decider_state == "open"
    asyncio.run(runner.run(_probe(), _msgs(1)))
    assert dec.calls == 4  # skipped again


def test_breaker_success_resets_the_failure_count_and_state_tracks_it() -> None:
    runner, _, dec, _ = _breaker()
    asyncio.run(runner.run(_probe(), _msgs(1)))
    asyncio.run(runner.run(_probe(), _msgs(1)))
    assert runner.decider_state == "degraded"
    dec.fail = False
    asyncio.run(runner.run(_probe(), _msgs(1)))
    assert runner.decider_state == "ok"
    dec.fail = True
    for _ in range(2):  # 2 < threshold: the success in between reset the streak
        asyncio.run(runner.run(_probe(), _msgs(1)))
    assert runner.decider_state == "degraded"


def test_decider_failure_log_names_the_exception_type(caplog) -> None:
    # httpx.ReadTimeout / TimeoutError have an EMPTY str(), which used to log "failed ()".
    runner, _, _, _ = _breaker()
    with caplog.at_level("WARNING", logger="viewlyt.live"):
        asyncio.run(runner.run(_probe(), _msgs(1)))
    assert any("TimeoutError" in r.getMessage() for r in caplog.records)


def test_hybrid_aclose_closes_both_clients_even_if_one_raises() -> None:
    runner, chat, dec, _ = _breaker()
    closed: list[str] = []

    async def dec_close():
        closed.append("decider")
        raise RuntimeError("boom")

    async def chat_close():
        closed.append("chat")

    dec.aclose = dec_close  # type: ignore[attr-defined]
    chat.aclose = chat_close  # type: ignore[attr-defined]
    asyncio.run(runner.aclose())
    assert sorted(closed) == ["chat", "decider"]


# --- item 7: host matching -------------------------------------------------------------


def test_decisions_url_rejects_lookalike_hosts() -> None:
    for bad in (
        "https://openrouter.ai.corp-gw.example/v1",
        "https://evilopenrouter.ai/v1",
        "https://notopenrouter.ai/api",
        "https://api.perplexity.ai.evil.example/v1",
        "https://example.com/openrouter.ai",
        "https://perplexity.ai/v1",
    ):
        assert decisions_url(bad) is None, bad
    assert decisions_url("https://sub.openrouter.ai/api/v1") == (
        "https://openrouter.ai/api/alpha/decisions"
    )


# --- item 9: Perplexity budget ---------------------------------------------------------


def test_cost_is_estimated_when_the_response_carries_none() -> None:
    usage = {"usage": {"input_tokens": 1_000_000, "output_tokens": 500}}
    assert usage_from_response(usage, "pplx-decider-flash-v1.2") == (1_000_500, 0.01)
    assert usage_from_response(usage, "perplexity/pplx-decider-flash-v1.2")[1] == 0.01
    assert usage_from_response(usage, "pplx-decider-flash-latest")[1] == 0.01
    assert usage_from_response(usage, "pplx-decider-latest")[1] == 0.02
    assert usage_from_response(usage, "Perplexity/PPLX-Decider-V1.2")[1] == 0.02
    assert usage_from_response(usage, "some/other-decider")[1] == 0.0
    assert usage_from_response(usage)[1] == 0.0
    # A reported cost always wins over the estimate.
    paid = {"usage": {"input_tokens": 1_000_000, "cost": 0.5}}
    assert usage_from_response(paid, "pplx-decider-flash-v1.2")[1] == 0.5


def test_perplexity_client_accumulates_the_estimated_cost() -> None:
    httpx = pytest.importorskip("httpx")

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(
            200, json={"answers": _ok_answers(body), "usage": {"input_tokens": 2_000_000}}
        )

    client = _mock_client(
        handler, base_url="https://api.perplexity.ai", model="pplx-decider-flash-latest"
    )
    asyncio.run(client.classify(_probe(), _msgs(2)))
    assert client.total_cost == pytest.approx(0.02)


# --- item 10: attribution headers ------------------------------------------------------


def test_attribution_headers_only_go_to_openrouter() -> None:
    httpx = pytest.importorskip("httpx")
    seen: list = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"answers": _ok_answers(json.loads(request.content))})

    asyncio.run(_mock_client(handler).classify(_probe(), _msgs(1)))
    asyncio.run(
        _mock_client(handler, base_url="https://api.perplexity.ai").classify(_probe(), _msgs(1))
    )
    or_req, px_req = seen
    assert or_req.headers["X-Title"] == "viewlyt" and "HTTP-Referer" in or_req.headers
    assert "X-Title" not in px_req.headers and "HTTP-Referer" not in px_req.headers
    assert px_req.headers["Authorization"] == "Bearer sk-test"
    assert str(px_req.url) == "https://api.perplexity.ai/v1/decisions"


# --- item 13: token overhead + provider ------------------------------------------------


def test_probe_question_is_sent_once_per_request_not_once_per_message() -> None:
    (req,) = build_decision_requests(_probe(), _msgs(32), model="m")
    assert json.dumps(req["questions"]).count("How does the author feel?") == 0
    assert json.dumps(req).count("How does the author feel?") == 1  # in state only
    assert "criteria" in req["questions"]["m0"]


def test_perplexity_is_a_reachable_provider() -> None:
    from viewlyt.live.cli import build_parser
    from viewlyt.live.llm import PROVIDERS, provider_base_url

    assert PROVIDERS["perplexity"] == "https://api.perplexity.ai"
    assert provider_base_url("perplexity") == "https://api.perplexity.ai"
    assert decisions_url(provider_base_url("perplexity")) == (
        "https://api.perplexity.ai/v1/decisions"
    )
    assert build_parser().parse_args(["--provider", "perplexity"]).provider == "perplexity"


# --- item 6: public dict ---------------------------------------------------------------


def test_public_dict_reports_whether_the_decider_is_actually_active() -> None:
    from viewlyt.live.llm import LLMConfig

    d = DEFAULT_DECIDER_MODEL
    local = LLMConfig(base_url="http://localhost:1234/v1", decider_model=d)
    assert local.to_public_dict()["decider_active"] is False  # no decisions endpoint
    unset = LLMConfig(base_url="https://openrouter.ai/api/v1")
    assert unset.to_public_dict()["decider_active"] is False  # nothing configured
    cfg = LLMConfig(base_url="https://openrouter.ai/api/v1", decider_model=d)
    assert cfg.to_public_dict()["decider_active"] is True
    lookalike = LLMConfig(base_url="https://evilopenrouter.ai/v1", decider_model=d)
    assert lookalike.to_public_dict()["decider_active"] is False


# --- item 5: dependency ----------------------------------------------------------------


def test_live_extra_declares_httpx() -> None:
    # decisions.py imports httpx directly; it must not rely on openai pulling it in.
    import tomllib

    root = Path(__file__).resolve().parent.parent
    live = tomllib.loads((root / "pyproject.toml").read_text())["project"]["optional-dependencies"][
        "live"
    ]
    assert any(dep.replace(" ", "").startswith("httpx>=") for dep in live)
