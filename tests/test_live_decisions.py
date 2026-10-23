"""Decision-model backend: pure request/response mapping, the HTTP client (via
``httpx.MockTransport``), and :class:`HybridRunner` routing. No network, no browser."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewlyt.live.decisions import (  # noqa: E402
    DECIDER_BATCH,
    DEFAULT_DECIDER_MODEL,
    build_decision_requests,
    decisions_url,
    labels_from_answers,
    usage_from_response,
)
from viewlyt.live.messages import ChatMessage  # noqa: E402
from viewlyt.live.probes import ClassificationProbe, OpenSummaryProbe  # noqa: E402


def _msgs(n: int) -> list[ChatMessage]:
    return [ChatMessage(author=f"u{i}", text=f"text number {i}", ts=float(i)) for i in range(n)]


def _probe(categories: list[str] | None = None) -> ClassificationProbe:
    return ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does the author feel?",
        categories=["happy", "sad", "neutral"] if categories is None else categories,
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_decisions_url_mapping() -> None:
    assert decisions_url("https://openrouter.ai/api/v1") == (
        "https://openrouter.ai/api/alpha/decisions"
    )
    assert decisions_url("https://api.perplexity.ai") == "https://api.perplexity.ai/v1/decisions"
    assert decisions_url("http://localhost:1234/v1") is None
    assert decisions_url("https://api.openai.com/v1") is None
    assert decisions_url("") is None


def test_decisions_url_is_case_insensitive_on_host() -> None:
    assert decisions_url("https://OpenRouter.AI/api/v1") == (
        "https://openrouter.ai/api/alpha/decisions"
    )
    assert decisions_url("https://API.Perplexity.AI/x") == "https://api.perplexity.ai/v1/decisions"


def test_build_requests_batches_with_global_indices() -> None:
    probe = _probe()
    reqs = build_decision_requests(probe, _msgs(33), model="m", batch_size=32)
    assert len(reqs) == 2
    assert list(reqs[0]["questions"]) == [f"m{i}" for i in range(32)]
    assert list(reqs[1]["questions"]) == ["m32"]
    assert all(r["model"] == "m" for r in reqs)
    for r in reqs:
        for q in r["questions"].values():
            assert q["type"] == "choice"
            assert list(q["criteria"]) == probe.categories
    assert "text number 32" in reqs[1]["questions"]["m32"]["instructions"]
    assert "How does the author feel?" in reqs[0]["questions"]["m0"]["instructions"]
    state = reqs[0]["state"]
    assert state["task"] == probe.question
    assert state["categories"] == probe.categories
    assert "untrusted" in state["note"]


def test_default_batch_size_is_the_documented_constant() -> None:
    assert DECIDER_BATCH == 32
    assert len(build_decision_requests(_probe(), _msgs(64), model="m")) == 2
    assert DEFAULT_DECIDER_MODEL == "perplexity/pplx-decider-flash-v1.2"


def test_build_requests_clamps_batch_size() -> None:
    assert len(build_decision_requests(_probe(), _msgs(3), model="m", batch_size=0)) == 3
    assert len(build_decision_requests(_probe(), _msgs(200), model="m", batch_size=999)) == 2


def test_build_requests_pair_categories_kept_verbatim() -> None:
    probe = _probe(["(2, 1)", "(1, 1)", "outro"])
    (req,) = build_decision_requests(probe, _msgs(1), model="m")
    assert list(req["questions"]["m0"]["criteria"]) == ["(2, 1)", "(1, 1)", "outro"]


def test_build_requests_empty_messages_and_empty_categories() -> None:
    assert build_decision_requests(_probe(), [], model="m") == []
    with pytest.raises(ValueError):
        build_decision_requests(_probe([]), _msgs(2), model="m")


def test_labels_from_answers_tolerates_missing_and_wrong_types() -> None:
    responses = [
        {"answers": {"m0": {"type": "choice", "choice": "happy"}}},
        {
            "answers": {
                "m2": {"type": "number", "choice": "sad"},  # wrong type
                "m3": {"type": "choice", "choice": ""},  # empty
                "m4": {"type": "choice", "choice": 7},  # not a str
                "m5": {"type": "choice", "choice": "sad"},
            }
        },
        {},  # no answers at all
    ]
    assert labels_from_answers(responses, 6) == ["happy", "", "", "", "", "sad"]
    assert labels_from_answers([], 2) == ["", ""]


def test_usage_from_response_never_raises() -> None:
    assert usage_from_response(
        {"usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.5}}
    ) == (
        12,
        0.5,
    )
    assert usage_from_response({"usage": {"input_tokens": 10}}) == (10, 0.0)  # no cost
    assert usage_from_response({}) == (0, 0.0)
    assert usage_from_response({"usage": {"input_tokens": "x"}}) == (0, 0.0)


# ---------------------------------------------------------------------------
# DecisionClient (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _client_and_seen(handler_status: int = 200):
    pytest.importorskip("httpx")
    import httpx

    from viewlyt.live.decisions import DecisionClient

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if handler_status != 200:
            return httpx.Response(handler_status, json={"error": "nope"})
        body = json.loads(request.content)
        answers = {
            qid: {"type": "choice", "choice": "happy" if int(qid[1:]) % 2 == 0 else "sad"}
            for qid in body["questions"]
        }
        return httpx.Response(
            200,
            json={
                "answers": answers,
                "usage": {"input_tokens": 100, "output_tokens": 5, "cost": 0.001},
            },
        )

    client = DecisionClient(
        "https://openrouter.ai/api/v1",
        "sk-test",
        DEFAULT_DECIDER_MODEL,
        transport=httpx.MockTransport(handler),
    )
    return client, seen


def test_classify_merges_batches_in_order_and_accumulates_usage() -> None:
    client, seen = _client_and_seen()
    out = asyncio.run(client.classify(_probe(), _msgs(33)))
    assert len(seen) == 2
    assert str(seen[0].url) == "https://openrouter.ai/api/alpha/decisions"
    assert seen[0].headers["Authorization"] == "Bearer sk-test"
    assert out["labels"] == ["happy" if i % 2 == 0 else "sad" for i in range(33)]
    assert client.total_tokens == 2 * 105
    assert client.total_cost == pytest.approx(0.002)
    assert client.avg_latency_ms is not None
    # The aggregate step consumes the result unchanged.
    res = _probe().aggregate(out, _msgs(33))
    assert res.pct["happy"] > 0 and res.pct["sad"] > 0


def test_classify_http_error_raises() -> None:
    httpx = pytest.importorskip("httpx")
    client, _ = _client_and_seen(401)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(client.classify(_probe(), _msgs(2)))
    assert client.total_tokens == 0


def test_client_rejects_unsupported_base_url() -> None:
    pytest.importorskip("httpx")
    from viewlyt.live.decisions import DecisionClient

    with pytest.raises(ValueError):
        DecisionClient("http://localhost:1234/v1", "k", "m")


def test_avg_latency_none_before_any_call() -> None:
    client, _ = _client_and_seen()
    assert client.avg_latency_ms is None


# ---------------------------------------------------------------------------
# HybridRunner routing
# ---------------------------------------------------------------------------


class _FakeChat:
    model = "chat-model"

    def __init__(self) -> None:
        self.run_calls: list[str] = []
        self.json_calls = 0
        self.total_tokens = 10
        self.total_cost = 0.5
        self._latency_count = 2
        self._latency_sum_ms = 200.0

    async def run(self, probe, messages) -> dict:
        self.run_calls.append(probe.id)
        if probe.kind == "classification":
            return {"labels": ["chat"] * len(messages)}
        return {"summary": "from chat"}

    async def complete_json(self, system, user, schema) -> dict:
        self.json_calls += 1
        return {"ok": True}


class _FakeDecider:
    model = "decider-model"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.total_tokens = 5
        self.total_cost = 0.25
        self._latency_count = 2
        self._latency_sum_ms = 400.0

    async def classify(self, probe, messages) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return {"labels": ["decider"] * len(messages)}


def _hybrid(decider: _FakeDecider | None = None):
    from viewlyt.live.llm import HybridRunner

    chat = _FakeChat()
    dec = decider or _FakeDecider()
    return HybridRunner(chat, dec), chat, dec  # type: ignore[arg-type]


def test_hybrid_routes_classification_to_decider() -> None:
    runner, chat, dec = _hybrid()
    out = asyncio.run(runner.run(_probe(), _msgs(3)))
    assert out == {"labels": ["decider"] * 3}
    assert dec.calls == 1 and chat.run_calls == []


def test_hybrid_falls_back_to_chat_when_decider_fails(caplog) -> None:
    runner, chat, dec = _hybrid(_FakeDecider(fail=True))
    with caplog.at_level("WARNING", logger="viewlyt.live"):
        out = asyncio.run(runner.run(_probe(), _msgs(2)))
    assert out == {"labels": ["chat"] * 2}
    assert dec.calls == 1 and chat.run_calls == ["mood"]
    assert any("falling back to the chat model" in r.message for r in caplog.records)


def test_hybrid_open_probe_and_complete_json_stay_on_chat() -> None:
    runner, chat, dec = _hybrid()
    open_probe = OpenSummaryProbe(id="sum", label="S", instruction="x")
    assert asyncio.run(runner.run(open_probe, _msgs(2))) == {"summary": "from chat"}
    assert asyncio.run(runner.complete_json("s", "u", {})) == {"ok": True}
    assert dec.calls == 0 and chat.json_calls == 1


def test_hybrid_classification_without_categories_uses_chat() -> None:
    runner, chat, dec = _hybrid()
    asyncio.run(runner.run(_probe([]), _msgs(2)))
    assert dec.calls == 0 and chat.run_calls == ["mood"]


def test_hybrid_counters_are_summed() -> None:
    runner, _, _ = _hybrid()
    assert runner.total_tokens == 15
    assert runner.total_cost == pytest.approx(0.75)
    assert runner.avg_latency_ms == 150  # (200 + 400) / 4 calls
    assert runner.model == "chat-model"


def test_hybrid_avg_latency_none_without_calls() -> None:
    runner, chat, dec = _hybrid()
    chat._latency_count = dec._latency_count = 0
    assert runner.avg_latency_ms is None


# ---------------------------------------------------------------------------
# build_runner
# ---------------------------------------------------------------------------


def test_build_runner_plain_chat_when_no_decider() -> None:
    pytest.importorskip("openai")
    from viewlyt.live.llm import LLMClient, LLMConfig, build_runner

    r = build_runner(LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="k"))
    assert type(r) is LLMClient


def test_build_runner_ignores_decider_on_unsupported_base_url(caplog) -> None:
    pytest.importorskip("openai")
    from viewlyt.live.llm import LLMClient, LLMConfig, build_runner

    cfg = LLMConfig(base_url="http://localhost:1234/v1", decider_model="x/decider")
    with caplog.at_level("WARNING", logger="viewlyt.live"):
        r = build_runner(cfg)
    assert type(r) is LLMClient
    assert any("ignored" in rec.message for rec in caplog.records)


def test_build_runner_hybrid_on_openrouter() -> None:
    pytest.importorskip("openai")
    pytest.importorskip("httpx")
    from viewlyt.live.llm import HybridRunner, LLMConfig, build_runner

    cfg = LLMConfig(
        base_url="https://openrouter.ai/api/v1", api_key="k", decider_model=DEFAULT_DECIDER_MODEL
    )
    r = build_runner(cfg)
    assert isinstance(r, HybridRunner)
    assert r.decider.model == DEFAULT_DECIDER_MODEL


def test_llm_config_public_dict_carries_decider_model() -> None:
    from viewlyt.live.llm import LLMConfig

    assert LLMConfig().to_public_dict()["decider_model"] == ""
    assert LLMConfig(decider_model="d/m").to_public_dict()["decider_model"] == "d/m"
