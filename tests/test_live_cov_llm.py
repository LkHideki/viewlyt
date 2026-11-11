"""Behavior tests for ``viewlyt.live.llm`` (no network, no browser).

Every LLM boundary is faked: ``LLMClient`` runs against a scripted stand-in for
``openai.AsyncOpenAI`` (monkeypatched), ``HybridRunner`` against fake chat/decider
objects with an injected clock, and the rewrite/suggest/decompose helpers against a
fake ``LLMRunner``. No sleeps beyond event-loop yields.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from viewlyt.live import llm
from viewlyt.live.llm import (
    CHART_TYPES,
    PROVIDERS,
    HybridRunner,
    LLMClient,
    LLMConfig,
    _clamp_int,
    _clean_categories,
    _normalize_suggested_probe,
    build_runner,
    decompose_probe,
    parse_json_loose,
    provider_base_url,
    rewrite_probe_spec,
    run_probes,
    suggest_probes,
)
from viewlyt.live.messages import ChatMessage
from viewlyt.live.probes import ClassificationProbe, OpenSummaryProbe, probe_from_dict


def _msgs(n: int = 3) -> list[ChatMessage]:
    return [ChatMessage(author=f"u{i}", text=f"msg {i}", ts=float(i)) for i in range(n)]


def _cls(pid: str = "c1", cats: list[str] | None = None) -> ClassificationProbe:
    return ClassificationProbe(pid, "Mood", "How do they feel?", cats or ["good", "bad"])


def _open(pid: str = "o1") -> OpenSummaryProbe:
    return OpenSummaryProbe(pid, "Topics", "Summarize the topics", max_words=40)


# ---------------------------------------------------------------------------
# provider_base_url / LLMConfig
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(PROVIDERS))
def test_provider_base_url_known_keys(key: str) -> None:
    assert provider_base_url(key) == PROVIDERS[key]


def test_provider_base_url_unknown_falls_back_to_lmstudio() -> None:
    assert provider_base_url("nope") == "http://localhost:1234/v1"
    assert provider_base_url("") == "http://localhost:1234/v1"


def test_to_public_dict_hides_api_key_and_flags_active_decider() -> None:
    cfg = LLMConfig(
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-secret",
        decider_model="vendor/decider",
        budget_usd=2.5,
    )
    pub = cfg.to_public_dict()
    assert "sk-secret" not in json.dumps(pub)
    assert "api_key" not in pub
    assert pub["budget"] == 2.5
    assert pub["decider_model"] == "vendor/decider"
    assert pub["decider_active"] is True


def test_to_public_dict_decider_inactive_without_endpoint_or_model() -> None:
    # Configured but the provider has no decisions endpoint -> not "active".
    no_endpoint = LLMConfig(base_url="https://api.openai.com/v1", decider_model="m")
    assert no_endpoint.to_public_dict()["decider_active"] is False
    # Endpoint exists but no decider model chosen.
    no_model = LLMConfig(base_url="https://openrouter.ai/api/v1", decider_model="")
    assert no_model.to_public_dict()["decider_active"] is False


# ---------------------------------------------------------------------------
# parse_json_loose
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", {}),
        (None, {}),
        ('{"a": 1}', {"a": 1}),
        ('["x", "y"]', {"labels": ["x", "y"]}),  # bare list -> classification shape
        ("42", {}),  # valid JSON but neither object nor list
        ('"just a string"', {}),
        ("null", {}),
        ("not json at all", {}),
        ('Sure! Here you go: {"summary": "hi"} hope it helps', {"summary": "hi"}),
        ('```json\n{"labels": ["a", "b"]}\n```', {"labels": ["a", "b"]}),
        ('Result: ["a", "b"] done', {"labels": ["a", "b"]}),  # embedded array
        ('{"a": 1,}', {}),  # block found but malformed -> {}
        ("prefix {broken", {}),
        ("[1, 2", {}),
    ],
)
def test_parse_json_loose(content: str | None, expected: dict) -> None:
    assert parse_json_loose(content) == expected  # type: ignore[arg-type]


def test_parse_json_loose_multiline_embedded_object() -> None:
    text = 'Here:\n{\n  "summary": "multi\\nline",\n  "n": 2\n}\nBye'
    assert parse_json_loose(text) == {"summary": "multi\nline", "n": 2}


# ---------------------------------------------------------------------------
# Fake AsyncOpenAI + LLMClient
# ---------------------------------------------------------------------------


class _HTTPError(Exception):
    """Mimics openai.APIStatusError: carries ``status_code``."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.status_code = status_code


def _resp(
    content: str | None = '{"summary": "ok"}', usage: object | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
    )


class FakeOpenAI:
    """Scripted stand-in for ``openai.AsyncOpenAI``.

    ``script`` is a list of responses or exceptions consumed one per ``create`` call;
    when it runs out the last item repeats. Every call's kwargs are recorded in ``calls``.
    """

    instances: list[FakeOpenAI] = []

    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs
        self.calls: list[dict] = []
        self.script: list[object] = [_resp()]
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.closed = False
        FakeOpenAI.instances.append(self)

    async def _create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def fake_openai(monkeypatch: pytest.MonkeyPatch) -> type[FakeOpenAI]:
    openai = pytest.importorskip("openai")
    FakeOpenAI.instances = []
    monkeypatch.setattr(openai, "AsyncOpenAI", FakeOpenAI)
    return FakeOpenAI


def _client(cfg: LLMConfig | None = None, script: list[object] | None = None) -> LLMClient:
    c = LLMClient(cfg or LLMConfig(base_url="http://localhost:1234/v1", model="m1"))
    if script is not None:
        FakeOpenAI.instances[-1].script = script
    return c


def _fake(c: LLMClient) -> FakeOpenAI:
    return c._client  # type: ignore[return-value]


def _modes(calls: list[dict]) -> list[str]:
    out = []
    for call in calls:
        rf = call.get("response_format")
        out.append("plain" if rf is None else rf["type"])
    return out


def test_ctor_openrouter_adds_headers_and_usage_extra(fake_openai) -> None:
    LLMClient(LLMConfig(base_url="https://openrouter.ai/api/v1", api_key="k", timeout=7.0))
    kw = fake_openai.instances[-1].init_kwargs
    assert kw["base_url"] == "https://openrouter.ai/api/v1"
    assert kw["api_key"] == "k"
    assert kw["timeout"] == 7.0
    assert kw["default_headers"]["X-Title"] == "viewlyt"


def test_ctor_local_has_no_headers_and_dummy_key(fake_openai) -> None:
    c = LLMClient(LLMConfig(base_url="http://localhost:1234/v1", api_key=""))
    kw = fake_openai.instances[-1].init_kwargs
    assert kw["api_key"] == "x"  # openai SDK refuses an empty key
    assert "default_headers" not in kw
    assert c.model == c.cfg.model


def test_openrouter_requests_include_usage_extra_body(fake_openai) -> None:
    c = _client(LLMConfig(base_url="https://openrouter.ai/api/v1"))
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert _fake(c).calls[0]["extra_body"] == {"usage": {"include": True}}


def test_local_requests_have_no_extra_body(fake_openai) -> None:
    c = _client()
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert "extra_body" not in _fake(c).calls[0]


def test_first_attempt_is_json_schema_with_temperature_zero(fake_openai) -> None:
    c = _client()
    schema = {"name": "n", "strict": True, "schema": {}}
    out = asyncio.run(c.complete_json("s", "u", schema))
    call = _fake(c).calls[0]
    assert out == {"summary": "ok"}
    assert call["model"] == "m1"
    assert call["temperature"] == 0
    assert call["response_format"] == {"type": "json_schema", "json_schema": schema}


def test_format_fallback_chain_on_400_then_success(fake_openai) -> None:
    c = _client(script=[_HTTPError(400), _resp('{"x": 1}')])
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"x": 1}
    assert _modes(_fake(c).calls) == ["json_schema", "json_object"]


@pytest.mark.parametrize("status", [400, 404, 422])
def test_format_rejection_statuses_all_fall_through(fake_openai, status: int) -> None:
    c = _client(script=[_HTTPError(status), _HTTPError(status), _resp('{"x": 2}')])
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"x": 2}
    assert _modes(_fake(c).calls) == ["json_schema", "json_object", "plain"]
    # the plain attempt carries no response_format at all
    assert "response_format" not in _fake(c).calls[2]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_other_http_errors_fail_fast_without_trying_other_modes(fake_openai, status: int) -> None:
    c = _client(script=[_HTTPError(status), _resp()])
    with pytest.raises(_HTTPError) as ei:
        asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert ei.value.status_code == status
    assert len(_fake(c).calls) == 1
    # nothing is remembered from a failure: the next request starts from the top again
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"summary": "ok"}
    assert _modes(_fake(c).calls) == ["json_schema", "json_schema"]


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionError("down"), ValueError("x")])
def test_errors_without_status_code_fail_fast(fake_openai, exc: Exception) -> None:
    c = _client(script=[exc, _resp()])
    with pytest.raises(type(exc)):
        asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert len(_fake(c).calls) == 1


def test_all_modes_rejected_raises_last_exception(fake_openai) -> None:
    errs = [_HTTPError(400), _HTTPError(404), _HTTPError(422)]
    c = _client(script=list(errs))
    with pytest.raises(_HTTPError) as ei:
        asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert ei.value is errs[2]
    assert _modes(_fake(c).calls) == ["json_schema", "json_object", "plain"]


def test_non_format_error_mid_chain_stops_immediately(fake_openai) -> None:
    c = _client(script=[_HTTPError(400), TimeoutError("t"), _resp()])
    with pytest.raises(TimeoutError):
        asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert _modes(_fake(c).calls) == ["json_schema", "json_object"]


def test_cached_mode_is_tried_first_on_next_request(fake_openai) -> None:
    c = _client(script=[_HTTPError(400), _resp('{"a": 1}'), _resp('{"a": 2}')])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"a": 2}
    # 2 calls for the first request, then straight to the cached json_object
    assert _modes(_fake(c).calls) == ["json_schema", "json_object", "json_object"]


def test_cached_plain_mode_skips_dead_attempts(fake_openai) -> None:
    c = _client(script=[_HTTPError(422), _HTTPError(422), _resp('{"a": 1}'), _resp('{"a": 2}')])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert _modes(_fake(c).calls)[3:] == ["plain"]


def test_cached_mode_failure_falls_back_in_stable_order(fake_openai) -> None:
    # Cache json_object, then the provider starts rejecting it with 400.
    c = _client(
        script=[
            _HTTPError(400),
            _resp('{"a": 1}'),
            _HTTPError(400),
            _resp('{"a": 3}'),
            _resp('{"a": 4}'),
        ]
    )
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"a": 3}
    # cached first, then the remaining modes keep their original relative order
    assert _modes(_fake(c).calls)[2:] == ["json_object", "json_schema"]
    # the mode that finally worked is now the cached one
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"a": 4}
    assert _modes(_fake(c).calls)[4:] == ["json_schema"]


def test_empty_or_missing_content_parses_to_empty_dict(fake_openai) -> None:
    c = _client(script=[_resp(None)])
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {}


def test_fenced_json_reply_is_recovered(fake_openai) -> None:
    c = _client(script=[_resp('```json\n{"labels": ["a"]}\n```')])
    assert asyncio.run(c.complete_json("s", "u", {"name": "n"})) == {"labels": ["a"]}


# --- usage / cost / latency ---------------------------------------------------


def test_usage_total_tokens_and_cost_attribute_accumulate(fake_openai) -> None:
    u1 = SimpleNamespace(total_tokens=100, cost=0.01)
    u2 = SimpleNamespace(total_tokens=50, cost=0.005)
    c = _client(script=[_resp(usage=u1), _resp(usage=u2)])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.total_tokens == 150
    assert c.total_cost == pytest.approx(0.015)


def test_usage_tokens_fall_back_to_prompt_plus_completion(fake_openai) -> None:
    u = SimpleNamespace(total_tokens=0, prompt_tokens=30, completion_tokens=12)
    c = _client(script=[_resp(usage=u)])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.total_tokens == 42
    assert c.total_cost == 0.0  # provider reported no cost


def test_usage_cost_read_from_model_extra(fake_openai) -> None:
    u = SimpleNamespace(total_tokens=10, model_extra={"cost": 0.0042})
    c = _client(script=[_resp(usage=u)])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.total_cost == pytest.approx(0.0042)


def test_usage_explicit_zero_cost_wins_over_model_extra(fake_openai) -> None:
    u = SimpleNamespace(total_tokens=10, cost=0.0, model_extra={"cost": 9.0})
    c = _client(script=[_resp(usage=u)])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.total_cost == 0.0


def test_usage_none_model_extra_and_missing_usage_are_ignored(fake_openai) -> None:
    c = _client(
        script=[
            _resp(usage=None),
            _resp(usage=SimpleNamespace(total_tokens=5, model_extra=None)),
        ]
    )
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert (c.total_tokens, c.total_cost) == (0, 0.0)
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert (c.total_tokens, c.total_cost) == (5, 0.0)


@pytest.mark.parametrize(
    "resp",
    [
        _resp(usage=SimpleNamespace(total_tokens=0, prompt_tokens=None, completion_tokens=3)),
        _resp(usage=SimpleNamespace(total_tokens="abc")),
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"k": 1}'))]),
    ],
    ids=["none-prompt", "non-numeric-tokens", "no-usage-attr"],
)
def test_garbage_usage_never_raises_nor_corrupts_totals(fake_openai, resp) -> None:
    c = _client(script=[resp])
    out = asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert out in ({"summary": "ok"}, {"k": 1})  # the answer still gets through
    assert (c.total_tokens, c.total_cost) == (0, 0.0)


class _FakeTime:
    """Drop-in for the ``time`` module inside ``llm``: only ``monotonic``.

    The clock moves only when the (fake) provider "takes time" to answer, so the
    measured latency never depends on how many times the client reads the clock.
    """

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now


def _slow_provider(c: LLMClient, clock: _FakeTime, delays: list[float]) -> None:
    """Make the i-th ``create`` call take ``delays[i]`` seconds of fake time."""
    fake = _fake(c)
    inner = fake.chat.completions.create
    queue = iter(delays)

    async def create(**kwargs: object) -> object:
        clock.now += next(queue)
        return await inner(**kwargs)

    fake.chat.completions.create = create  # type: ignore[assignment]


def test_avg_latency_none_without_calls_then_rounded_mean(fake_openai, monkeypatch) -> None:
    c = _client()
    assert c.avg_latency_ms is None
    clock = _FakeTime()
    monkeypatch.setattr(llm, "time", clock)
    # binary-exact fractions: 125 ms, 125 ms, 250 ms
    _slow_provider(c, clock, [0.125, 0.125, 0.25])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.avg_latency_ms == 125
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.avg_latency_ms == 125
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.avg_latency_ms == 167  # 500 / 3 = 166.67 -> rounded, not truncated


def test_failed_attempts_do_not_count_toward_latency(fake_openai, monkeypatch) -> None:
    c = _client(script=[_HTTPError(400), _resp()])
    clock = _FakeTime()
    monkeypatch.setattr(llm, "time", clock)
    # the rejected format attempt burns 9 s; only the successful 250 ms one is measured
    _slow_provider(c, clock, [9.0, 0.25])
    asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.avg_latency_ms == 250


def test_hard_failure_leaves_latency_unmeasured(fake_openai, monkeypatch) -> None:
    c = _client(script=[_HTTPError(500)])
    clock = _FakeTime()
    monkeypatch.setattr(llm, "time", clock)
    _slow_provider(c, clock, [3.0])
    with pytest.raises(_HTTPError):
        asyncio.run(c.complete_json("s", "u", {"name": "n"}))
    assert c.avg_latency_ms is None


# --- run() / complete_json() prompt assembly ----------------------------------


def test_run_open_probe_appends_language_instruction(fake_openai) -> None:
    c = _client(LLMConfig(base_url="http://localhost:1234/v1", language="French"))
    probe = _open()
    asyncio.run(c.run(probe, _msgs()))
    msgs = _fake(c).calls[0]["messages"]
    base_system, base_user = probe.build_prompt(_msgs())
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == f"{base_system}\n\nWrite the answer in French."
    assert msgs[1]["content"] == base_user
    assert _fake(c).calls[0]["response_format"]["json_schema"] == probe.output_schema()


def test_run_classification_probe_leaves_prompt_untouched(fake_openai) -> None:
    c = _client(LLMConfig(base_url="http://localhost:1234/v1", language="French"))
    probe = _cls()
    asyncio.run(c.run(probe, _msgs()))
    system, user = probe.build_prompt(_msgs())
    sent = _fake(c).calls[0]["messages"]
    assert sent[0]["content"] == system
    assert "French" not in sent[0]["content"]
    assert sent[1]["content"] == user
    assert _fake(c).calls[0]["response_format"]["json_schema"] == probe.output_schema()


def test_run_open_probe_without_language_is_untouched(fake_openai) -> None:
    c = _client(LLMConfig(base_url="http://localhost:1234/v1", language=""))
    probe = _open()
    asyncio.run(c.run(probe, _msgs()))
    assert _fake(c).calls[0]["messages"][0]["content"] == probe.build_prompt(_msgs())[0]


def test_run_returns_parsed_reply(fake_openai) -> None:
    c = _client(script=[_resp('{"labels": ["good", "bad", "good"]}')])
    assert asyncio.run(c.run(_cls(), _msgs())) == {"labels": ["good", "bad", "good"]}


def test_complete_json_language_suffix_and_message_layout(fake_openai) -> None:
    c = _client(LLMConfig(base_url="http://localhost:1234/v1", language="Spanish"))
    asyncio.run(c.complete_json("SYS", "USR", {"name": "n"}))
    msgs = _fake(c).calls[0]["messages"]
    assert msgs == [
        {"role": "system", "content": "SYS\n\nWrite any human-readable text in Spanish."},
        {"role": "user", "content": "USR"},
    ]


def test_complete_json_without_language_keeps_system_verbatim(fake_openai) -> None:
    c = _client(LLMConfig(base_url="http://localhost:1234/v1", language=""))
    asyncio.run(c.complete_json("SYS", "USR", {"name": "n"}))
    assert _fake(c).calls[0]["messages"][0]["content"] == "SYS"


# --- aclose ------------------------------------------------------------------


def test_aclose_awaits_async_closer(fake_openai) -> None:
    c = _client()
    asyncio.run(c.aclose())
    assert _fake(c).closed is True


def test_aclose_uses_sync_close_and_tolerates_missing_closer(fake_openai) -> None:
    c = _client()
    closed: list[str] = []
    c._client = SimpleNamespace(close=lambda: closed.append("sync"))  # type: ignore[assignment]
    asyncio.run(c.aclose())
    assert closed == ["sync"]
    c._client = SimpleNamespace()  # type: ignore[assignment]
    assert asyncio.run(c.aclose()) is None  # no closer: no-op, no raise
    assert closed == ["sync"]  # and nothing was closed a second time


# ---------------------------------------------------------------------------
# HybridRunner
# ---------------------------------------------------------------------------


class FakeChat:
    model = "chat-model"

    def __init__(self) -> None:
        self.total_tokens = 10
        self.total_cost = 0.5
        self._latency_count = 0
        self._latency_sum_ms = 0.0
        self.run_calls: list[str] = []
        self.json_calls: list[tuple] = []
        self.closed = False
        self.close_error: Exception | None = None

    async def run(self, probe, messages) -> dict:
        self.run_calls.append(probe.id)
        return {"via": "chat"}

    async def complete_json(self, system, user, schema) -> dict:
        self.json_calls.append((system, user, schema))
        return {"via": "chat-json"}

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeDecider:
    model = "decider-model"

    def __init__(self) -> None:
        self.total_tokens = 3
        self.total_cost = 0.25
        self._latency_count = 0
        self._latency_sum_ms = 0.0
        self.calls = 0
        self.fail = False
        self.closed = False
        self.close_error: Exception | None = None

    async def classify(self, probe, messages) -> dict:
        self.calls += 1
        if self.fail:
            raise RuntimeError("decider down")
        return {"via": "decider"}

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error:
            raise self.close_error


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hybrid() -> tuple[HybridRunner, FakeChat, FakeDecider, Clock]:
    chat, dec, clock = FakeChat(), FakeDecider(), Clock()
    return HybridRunner(chat, dec, clock=clock), chat, dec, clock  # type: ignore[arg-type]


def _run(h: HybridRunner, probe=None) -> dict:
    return asyncio.run(h.run(probe or _cls(), _msgs()))


def test_hybrid_routes_classification_to_decider() -> None:
    h, chat, dec, _ = _hybrid()
    assert _run(h) == {"via": "decider"}
    assert dec.calls == 1
    assert chat.run_calls == []
    assert h.decider_state == "ok"
    assert h.model == "chat-model"


def test_hybrid_open_and_empty_category_probes_stay_on_chat() -> None:
    h, chat, dec, _ = _hybrid()
    assert _run(h, _open()) == {"via": "chat"}
    empty = ClassificationProbe("e", "E", "q", [])
    assert _run(h, empty) == {"via": "chat"}
    assert dec.calls == 0
    assert chat.run_calls == ["o1", "e"]


def test_hybrid_complete_json_always_uses_chat() -> None:
    h, chat, dec, _ = _hybrid()
    out = asyncio.run(h.complete_json("s", "u", {"name": "n"}))
    assert out == {"via": "chat-json"}
    assert chat.json_calls == [("s", "u", {"name": "n"})]
    assert dec.calls == 0


def test_hybrid_single_failure_falls_back_and_marks_degraded(caplog) -> None:
    h, chat, dec, _ = _hybrid()
    dec.fail = True
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        assert _run(h) == {"via": "chat"}
    assert h.decider_state == "degraded"
    assert any("falling back to the chat model" in r.getMessage() for r in caplog.records)


def test_hybrid_success_resets_failure_streak() -> None:
    h, _, dec, _ = _hybrid()
    dec.fail = True
    _run(h)
    _run(h)
    assert h.decider_state == "degraded"
    dec.fail = False
    assert _run(h) == {"via": "decider"}
    assert h.decider_state == "ok"
    # the streak restarted from zero: two more failures still don't open it
    dec.fail = True
    _run(h)
    _run(h)
    assert h.decider_state == "degraded"


def test_hybrid_breaker_opens_after_three_failures_and_skips_decider(caplog) -> None:
    h, chat, dec, _ = _hybrid()
    dec.fail = True
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        for _i in range(3):
            assert _run(h) == {"via": "chat"}
    assert dec.calls == 3
    assert h.decider_state == "open"
    assert any("skipping it for 5 min" in r.getMessage() for r in caplog.records)
    # while open: decider is not touched, chat answers
    for _i in range(4):
        assert _run(h) == {"via": "chat"}
    assert dec.calls == 3
    assert len(chat.run_calls) == 7


def test_hybrid_breaker_stays_open_until_cooldown_elapses() -> None:
    h, _, dec, clock = _hybrid()
    dec.fail = True
    for _i in range(3):
        _run(h)
    clock.now += llm._BREAKER_COOLDOWN_S - 1
    assert h.decider_state == "open"
    _run(h)
    assert dec.calls == 3  # still skipped


def test_hybrid_half_open_failure_reopens_immediately() -> None:
    h, _, dec, clock = _hybrid()
    dec.fail = True
    for _i in range(3):
        _run(h)
    clock.now += llm._BREAKER_COOLDOWN_S
    assert _run(h) == {"via": "chat"}  # one probe call let through, fails
    assert dec.calls == 4
    assert h.decider_state == "open"  # a SINGLE failure re-opens
    _run(h)
    assert dec.calls == 4  # and it is skipped again


def test_hybrid_half_open_success_closes_for_good(caplog) -> None:
    h, _, dec, clock = _hybrid()
    dec.fail = True
    for _i in range(3):
        _run(h)
    clock.now += llm._BREAKER_COOLDOWN_S + 1
    dec.fail = False
    with caplog.at_level(logging.INFO, logger="viewlyt.live"):
        assert _run(h) == {"via": "decider"}
    assert h.decider_state == "ok"
    assert any("circuit closed" in r.getMessage() for r in caplog.records)
    # closed for good: it takes a full 3 fresh failures to trip again
    dec.fail = True
    _run(h)
    _run(h)
    assert h.decider_state == "degraded"


def test_hybrid_breaker_boundary_exact_cooldown_instant_is_not_open() -> None:
    h, _, dec, clock = _hybrid()
    dec.fail = True
    for _i in range(3):
        _run(h)
    opened_at = clock.now
    clock.now = opened_at + llm._BREAKER_COOLDOWN_S - 0.001
    assert h.decider_state == "open"  # a hair before the end: still open
    clock.now = opened_at + llm._BREAKER_COOLDOWN_S
    # at the exact cooldown instant the breaker already lets the decider through,
    # so the reported state must agree with the routing
    assert h.decider_state == "degraded"
    dec.fail = False
    assert _run(h) == {"via": "decider"}
    assert dec.calls == 4


def test_hybrid_state_after_cooldown_without_new_call_is_degraded() -> None:
    h, _, dec, clock = _hybrid()
    dec.fail = True
    for _i in range(3):
        _run(h)
    clock.now += llm._BREAKER_COOLDOWN_S + 1
    assert h.decider_state == "degraded"  # no longer "open", but failures not cleared yet


def test_hybrid_aggregates_tokens_cost_and_weighted_latency() -> None:
    h, chat, dec, _ = _hybrid()
    assert h.total_tokens == 13
    assert h.total_cost == pytest.approx(0.75)
    assert h.avg_latency_ms is None
    chat._latency_count, chat._latency_sum_ms = 1, 100.0
    dec._latency_count, dec._latency_sum_ms = 3, 500.0
    assert h.avg_latency_ms == 150  # (100 + 500) / 4, weighted by call count


def test_hybrid_aclose_closes_both_even_if_one_fails() -> None:
    h, chat, dec, _ = _hybrid()
    dec.close_error = RuntimeError("boom")
    asyncio.run(h.aclose())
    assert dec.closed and chat.closed
    h2, chat2, dec2, _ = _hybrid()
    chat2.close_error = RuntimeError("boom")
    asyncio.run(h2.aclose())
    assert dec2.closed and chat2.closed


# --- build_runner -------------------------------------------------------------


def test_build_runner_without_decider_returns_plain_client(fake_openai) -> None:
    r = build_runner(LLMConfig(base_url="https://openrouter.ai/api/v1"))
    assert isinstance(r, LLMClient)


def test_build_runner_decider_on_unsupported_provider_is_ignored_with_warning(
    fake_openai, caplog
) -> None:
    cfg = LLMConfig(base_url="https://api.openai.com/v1", decider_model="x/decider")
    with caplog.at_level(logging.WARNING, logger="viewlyt.live"):
        r = build_runner(cfg)
    assert isinstance(r, LLMClient)
    assert any("no decisions endpoint" in rec.getMessage() for rec in caplog.records)


def test_build_runner_hybrid_for_openrouter_and_perplexity(fake_openai) -> None:
    pytest.importorskip("httpx")
    from viewlyt.live.decisions import DECIDER_TIMEOUT, decisions_url

    for base in ("https://openrouter.ai/api/v1", PROVIDERS["perplexity"]):
        cfg = LLMConfig(base_url=base, api_key="k", model="chat/m", decider_model="dec/m")
        r = build_runner(cfg)
        assert isinstance(r, HybridRunner)
        assert r.model == "chat/m"
        assert r.decider.model == "dec/m"
        assert r.decider.url == decisions_url(base)
        assert r.decider.timeout == DECIDER_TIMEOUT  # own, shorter timeout


# ---------------------------------------------------------------------------
# run_probes
# ---------------------------------------------------------------------------


class RecordingClient:
    model = "x"

    def __init__(self) -> None:
        self.seen: dict[str, list[ChatMessage]] = {}
        self.fail_ids: set[str] = set()

    async def run(self, probe, messages) -> dict:
        self.seen[probe.id] = messages
        if probe.id in self.fail_ids:
            raise RuntimeError(f"boom {probe.id}")
        if probe.kind == "open":
            return {"summary": f"s-{probe.id}"}
        return {"labels": ["good"] * len(messages)}

    async def complete_json(self, system, user, schema) -> dict:
        return {}


def test_run_probes_empty_list_returns_empty() -> None:
    assert asyncio.run(run_probes(RecordingClient(), [], _msgs())) == []


def test_run_probes_preserves_input_order_and_aggregates() -> None:
    probes = [_open("a"), _cls("b"), _open("c")]
    res = asyncio.run(run_probes(RecordingClient(), probes, _msgs(4)))
    assert [r.probe_id for r in res] == ["a", "b", "c"]
    assert res[0].text == "s-a"
    assert res[1].kind == "classification"
    assert all(r.n == 4 for r in res)


def test_run_probes_per_probe_windows_override_default() -> None:
    client = RecordingClient()
    default, special = _msgs(5), _msgs(2)
    res = asyncio.run(run_probes(client, [_open("a"), _open("b")], default, windows={"b": special}))
    assert client.seen["a"] is default
    assert client.seen["b"] is special
    assert {r.probe_id: r.n for r in res} == {"a": 5, "b": 2}


def test_run_probes_failure_is_isolated_logged_and_dropped(caplog) -> None:
    client = RecordingClient()
    client.fail_ids = {"b"}
    probes = [_open("a"), _open("b"), _cls("c")]
    with caplog.at_level(logging.ERROR, logger="viewlyt.live"):
        res = asyncio.run(run_probes(client, probes, _msgs()))
    assert [r.probe_id for r in res] == ["a", "c"]
    assert any("probe 'b' failed" in r.getMessage() for r in caplog.records)


def test_run_probes_all_failing_yields_empty() -> None:
    client = RecordingClient()
    client.fail_ids = {"a", "b"}
    assert asyncio.run(run_probes(client, [_open("a"), _open("b")], _msgs())) == []


def test_run_probes_aggregation_error_is_also_isolated() -> None:
    class BadAggregate(OpenSummaryProbe):
        def aggregate(self, parsed, messages):
            raise ValueError("bad aggregate")

    probes = [BadAggregate("bad", "L", "i"), _open("ok")]
    res = asyncio.run(run_probes(RecordingClient(), probes, _msgs()))
    assert [r.probe_id for r in res] == ["ok"]


def test_run_probes_concurrency_is_bounded_but_parallel() -> None:
    limit = llm._PROBE_CONCURRENCY
    state = {"in_flight": 0, "peak": 0}

    class GatedClient(RecordingClient):
        def __init__(self) -> None:
            super().__init__()
            self.gate = asyncio.Event()

        async def run(self, probe, messages) -> dict:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            if state["in_flight"] >= limit:
                self.gate.set()
            await self.gate.wait()  # nobody finishes until `limit` are in flight at once
            await asyncio.sleep(0)
            state["in_flight"] -= 1
            return {"summary": "x"}

    probes = [_open(f"p{i}") for i in range(limit * 2 + 3)]

    async def go() -> list:
        # a too-small bound would leave the gate shut forever: fail fast instead of hanging
        return await asyncio.wait_for(run_probes(GatedClient(), probes, _msgs()), timeout=1.0)

    res = asyncio.run(go())
    assert len(res) == len(probes)
    assert state["peak"] == limit  # fanned out fully, never beyond the semaphore


# ---------------------------------------------------------------------------
# _clamp_int / _clean_categories
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(50, 50), ("42", 42), (5, 10), (10_000, 200), (-3, 10), ("abc", 60), (None, 60), ([1], 60)],
)
def test_clamp_int(value: object, expected: int) -> None:
    assert _clamp_int(value, 60, 10, 200) == expected


def test_clean_categories_lowercases_strips_and_dedupes() -> None:
    assert _clean_categories(["  Yes ", "NO", "yes", "", "   "]) == ["yes", "no"]


def test_clean_categories_non_list_input_is_empty() -> None:
    assert _clean_categories(None) == []
    assert _clean_categories("a,b") == []
    assert _clean_categories({"a": 1}) == []
    assert _clean_categories(("A", "b")) == ["a", "b"]  # tuples accepted


def test_clean_categories_coerces_non_string_items() -> None:
    assert _clean_categories([1, 2, 1]) == ["1", "2"]


def test_clean_categories_pairs_are_canonicalized_without_lowercasing() -> None:
    assert _clean_categories(["(  São Paulo ,Rio )"]) == ["(São Paulo, Rio)"]


def test_clean_categories_pair_dedup_is_case_and_accent_insensitive() -> None:
    out = _clean_categories(["(São Paulo, Alto)", "( sao  paulo , ALTO )", "(Sao Paulo, Baixo)"])
    assert out == ["(São Paulo, Alto)", "(Sao Paulo, Baixo)"]


# ---------------------------------------------------------------------------
# rewrite_probe_spec
# ---------------------------------------------------------------------------


class SpecClient:
    """Fake LLMRunner returning a scripted spec (or raising); records each prompt."""

    model = "x"

    def __init__(self, reply: object = None, exc: Exception | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls: list[tuple[str, str, dict]] = []

    async def run(self, probe, messages) -> dict:
        return {}

    async def complete_json(self, system: str, user: str, schema: dict):
        self.calls.append((system, user, schema))
        if self.exc:
            raise self.exc
        return self.reply


def _rewrite(client, kind, text="how do they feel?", cats=None) -> dict:
    return asyncio.run(rewrite_probe_spec(client, kind, text, cats))


def test_rewrite_open_happy_path_and_prompt() -> None:
    c = SpecClient({"instruction": "  Across all...  ", "label": " Mood ", "max_words": 80})
    spec = _rewrite(c, "open")
    assert spec == {"instruction": "Across all...", "label": "Mood", "max_words": 80}
    system, user, schema = c.calls[0]
    assert "SAMPLE of MANY" in system
    assert "User request: how do they feel?" in user
    assert schema["name"] == "open_probe_spec"


@pytest.mark.parametrize(("raw", "expected"), [(3, 10), (999, 200), ("x", 60), (None, 60)])
def test_rewrite_open_clamps_max_words(raw: object, expected: int) -> None:
    spec = _rewrite(SpecClient({"instruction": "i", "label": "l", "max_words": raw}), "open")
    assert spec["max_words"] == expected


def test_rewrite_open_exception_falls_back_to_raw_text() -> None:
    spec = _rewrite(SpecClient(exc=RuntimeError("llm down")), "open", text="what are they asking?")
    assert spec == {"instruction": "what are they asking?", "label": "", "max_words": 60}


@pytest.mark.parametrize("reply", [None, [], "junk", {"instruction": "  ", "label": None}])
def test_rewrite_open_non_dict_or_blank_reply_uses_text(reply: object) -> None:
    spec = _rewrite(SpecClient(reply), "open", text="raw ask")
    assert spec["instruction"] == "raw ask"
    assert spec["label"] == ""


def test_rewrite_open_ignores_caller_categories() -> None:
    c = SpecClient({"instruction": "i", "label": "l"})
    spec = _rewrite(c, "open", cats=["a", "b"])
    assert "categories" not in spec
    assert "kind" not in spec  # explicit kinds never return "kind"


def test_rewrite_classification_happy_path() -> None:
    c = SpecClient(
        {
            "question": " Sentiment? ",
            "categories": ["Happy", "Angry"],
            "label": " Mood ",
            "chart": "DONUT",
        }
    )
    spec = _rewrite(c, "classification")
    assert spec == {
        "question": "Sentiment?",
        "categories": ["happy", "angry"],
        "label": "Mood",
        "chart": "donut",
    }
    _system, user, schema = c.calls[0]
    assert schema["name"] == "classification_probe_spec"
    assert "Choose 3-6 mutually-exclusive" in user
    assert "descartes" in user and "(x, y)" in user  # the 2-D chart guidance is sent
    assert all(ch in user for ch in CHART_TYPES)


def test_rewrite_classification_caller_categories_win_and_prompt_pins_them() -> None:
    c = SpecClient({"question": "q", "categories": ["x", "y", "z"], "label": "l", "chart": "bars"})
    spec = _rewrite(c, "classification", cats=["Yes ", "no", "YES"])
    assert spec["categories"] == ["yes", "no"]
    user = c.calls[0][1]
    assert "you MUST keep them EXACTLY: yes, no" in user
    assert "Choose 3-6" not in user


@pytest.mark.parametrize("cats", [None, [], ["only-one"], ["a", "A"], "oops"])
def test_rewrite_classification_too_few_categories_default_to_sentiment(cats: object) -> None:
    spec = _rewrite(
        SpecClient({"question": "q", "categories": cats, "label": "l"}), "classification"
    )
    assert spec["categories"] == ["positive", "negative", "neutral"]


def test_rewrite_classification_single_caller_category_is_kept() -> None:
    # Caller choice always wins, even when it is a degenerate single category.
    spec = _rewrite(SpecClient({"categories": ["a", "b"]}), "classification", cats=["solo"])
    assert spec["categories"] == ["solo"]


@pytest.mark.parametrize("chart", ["pie", "", None, 5, "Bars "])
def test_rewrite_classification_chart_validation(chart: object) -> None:
    spec = _rewrite(SpecClient({"categories": ["a", "b"], "chart": chart}), "classification")
    assert spec["chart"] == "bars"


@pytest.mark.parametrize("chart", list(CHART_TYPES))
def test_rewrite_classification_accepts_every_known_chart(chart: str) -> None:
    spec = _rewrite(SpecClient({"categories": ["a", "b"], "chart": chart}), "classification")
    assert spec["chart"] == chart


def test_rewrite_classification_pair_categories_keep_case() -> None:
    cats = ["(Baixo, Ruim)", "(Alto, Bom)"]
    spec = _rewrite(
        SpecClient({"question": "q", "categories": cats, "label": "l", "chart": "descartes"}),
        "classification",
    )
    assert spec["categories"] == cats
    assert spec["chart"] == "descartes"


def test_rewrite_classification_exception_fallback_is_a_usable_probe() -> None:
    spec = _rewrite(SpecClient(exc=TimeoutError("t")), "classification", text="vibes?")
    assert spec == {
        "question": "vibes?",
        "categories": ["positive", "negative", "neutral"],
        "label": "",
        "chart": "bars",
    }
    probe = probe_from_dict({"kind": "classification", "id": "z", **spec})
    assert probe.categories == ["positive", "negative", "neutral"]


def test_rewrite_exception_with_caller_categories_keeps_them() -> None:
    spec = _rewrite(SpecClient(exc=RuntimeError()), "classification", cats=["A", "B"])
    assert spec["categories"] == ["a", "b"]


def test_rewrite_unknown_kind_is_treated_as_classification() -> None:
    c = SpecClient({"question": "q", "categories": ["a", "b"], "label": "l"})
    spec = _rewrite(c, "weird")
    assert c.calls[0][2]["name"] == "classification_probe_spec"
    assert "categories" in spec and "kind" not in spec


# --- auto -----------------------------------------------------------------------


def test_rewrite_auto_dispatch_uses_auto_schema_and_returns_kind() -> None:
    c = SpecClient({"kind": "open", "instruction": "Across all...", "label": "L", "max_words": 30})
    spec = _rewrite(c, "auto")
    assert c.calls[0][2]["name"] == "auto_probe_spec"
    assert c.calls[0][2]["schema"]["properties"]["kind"]["enum"] == ["open", "classification"]
    assert spec == {"kind": "open", "instruction": "Across all...", "max_words": 30, "label": "L"}


def test_rewrite_auto_classification_branch() -> None:
    c = SpecClient(
        {
            "kind": " Classification ",
            "question": "Which topic?",
            "categories": ["Music", "Game", "Music"],
            "label": "Topic",
            "chart": "columns",
        }
    )
    spec = _rewrite(c, "auto")
    assert spec == {
        "kind": "classification",
        "question": "Which topic?",
        "categories": ["music", "game"],
        "label": "Topic",
        "chart": "columns",
    }
    probe = probe_from_dict({"id": "auto1", **spec})
    assert probe.kind == "classification"


def test_rewrite_auto_classification_defaults_and_chart_validation() -> None:
    c = SpecClient({"kind": "classification", "categories": ["one"], "chart": "radar"})
    spec = _rewrite(c, "auto", text="raw ask")
    assert spec["question"] == "raw ask"
    assert spec["categories"] == ["positive", "negative", "neutral"]
    assert spec["chart"] == "bars"
    assert spec["label"] == ""


def test_rewrite_auto_caller_categories_are_exact_and_pinned_in_prompt() -> None:
    c = SpecClient({"kind": "classification", "categories": ["x", "y", "z"], "question": "q"})
    spec = _rewrite(c, "auto", cats=["Yes", "No"])
    assert spec["categories"] == ["yes", "no"]
    assert "you MUST keep them EXACTLY: yes, no" in c.calls[0][1]


def test_rewrite_auto_prompt_without_caller_categories_asks_for_3_to_6() -> None:
    c = SpecClient({"kind": "open"})
    _rewrite(c, "auto")
    user = c.calls[0][1]
    assert "pick 3-6 mutually-exclusive" in user
    assert "FIRST decide the best probe 'kind'" in user
    assert "descartes" in user


def test_rewrite_auto_caller_categories_ignored_when_model_picks_open() -> None:
    spec = _rewrite(SpecClient({"kind": "open", "instruction": "i"}), "auto", cats=["a", "b"])
    assert spec["kind"] == "open"
    assert "categories" not in spec


@pytest.mark.parametrize("kind", [None, "", "banana", 3])
def test_rewrite_auto_unknown_kind_defaults_to_open(kind: object) -> None:
    spec = _rewrite(SpecClient({"kind": kind, "instruction": "do it", "max_words": 5}), "auto")
    assert spec["kind"] == "open"
    assert spec["instruction"] == "do it"
    assert spec["max_words"] == 10  # clamped up


def test_rewrite_auto_open_clamps_max_words_high() -> None:
    spec = _rewrite(SpecClient({"kind": "open", "max_words": 5000}), "auto")
    assert spec["max_words"] == 200


@pytest.mark.parametrize("exc_or_reply", [RuntimeError("x"), [1, 2], None, "str"])
def test_rewrite_auto_failure_or_junk_falls_back_to_open_with_raw_text(exc_or_reply) -> None:
    c = (
        SpecClient(exc=exc_or_reply)
        if isinstance(exc_or_reply, Exception)
        else SpecClient(exc_or_reply)
    )
    spec = _rewrite(c, "auto", text="what is going on?")
    assert spec == {
        "kind": "open",
        "instruction": "what is going on?",
        "max_words": 60,
        "label": "",
    }


# ---------------------------------------------------------------------------
# _normalize_suggested_probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "x", 5, [], ["kind"]])
def test_normalize_non_dict_gives_usable_open_spec(raw: object) -> None:
    assert _normalize_suggested_probe(raw) == {
        "kind": "open",
        "instruction": "",
        "max_words": 60,
        "label": "",
    }


def test_normalize_classification_full() -> None:
    out = _normalize_suggested_probe(
        {
            "kind": "CLASSIFICATION",
            "label": " L ",
            "question": " Q ",
            "categories": ["A", "b", "a"],
            "chart": "Area",
        }
    )
    assert out == {
        "kind": "classification",
        "question": "Q",
        "categories": ["a", "b"],
        "label": "L",
        "chart": "area",
    }


def test_normalize_classification_defaults() -> None:
    out = _normalize_suggested_probe(
        {"kind": "classification", "categories": ["only"], "chart": "x"}
    )
    assert out["categories"] == ["positive", "negative", "neutral"]
    assert out["chart"] == "bars"
    assert out["question"] == ""  # NOT replaced by request text (there is none here)


def test_normalize_open_clamps_and_unknown_kind_is_open() -> None:
    out = _normalize_suggested_probe(
        {"kind": "mystery", "instruction": " go ", "max_words": "9999"}
    )
    assert out == {"kind": "open", "instruction": "go", "max_words": 200, "label": ""}


# ---------------------------------------------------------------------------
# suggest_probes / decompose_probe
# ---------------------------------------------------------------------------

_TWO = {
    "probes": [
        {"kind": "open", "label": "Topics", "instruction": "Across all...", "max_words": 40},
        {"kind": "classification", "label": "Mood", "question": "q", "categories": ["a", "b"]},
    ]
}


def test_suggest_probes_normalizes_two_specs_and_builds_prompt() -> None:
    c = SpecClient(_TWO)
    out = asyncio.run(suggest_probes(c, "  interesting stuff ", _msgs(2)))
    assert [p["kind"] for p in out] == ["open", "classification"]
    assert out[1]["chart"] == "bars"
    _system, user, schema = c.calls[0]
    assert "<request>interesting stuff</request>" in user
    assert "1. msg 0" in user and "2. msg 1" in user
    assert schema["name"] == "suggested_probes"
    assert schema["schema"]["properties"]["probes"]["minItems"] == 2


def test_suggest_probes_empty_request_and_empty_chat_use_placeholders() -> None:
    c = SpecClient(_TWO)
    asyncio.run(suggest_probes(c, "   ", []))
    user = c.calls[0][1]
    assert "no specific request" in user
    assert "(the chat sample is empty)" in user


def test_suggest_probes_truncates_to_two() -> None:
    three = {"probes": [{"kind": "open"}, {"kind": "open"}, {"kind": "open"}]}
    out = asyncio.run(suggest_probes(SpecClient(three), "x", _msgs()))
    assert len(out) == 2


@pytest.mark.parametrize(
    "reply", [{"probes": "nope"}, {"probes": None}, {}, [], None, "junk", {"probes": {"a": 1}}]
)
def test_suggest_probes_bad_shapes_yield_empty(reply: object) -> None:
    assert asyncio.run(suggest_probes(SpecClient(reply), "x", _msgs())) == []


def test_suggest_probes_exception_yields_empty() -> None:
    assert asyncio.run(suggest_probes(SpecClient(exc=RuntimeError("x")), "x", _msgs())) == []


def test_suggest_probes_junk_entries_are_normalized_not_dropped() -> None:
    out = asyncio.run(suggest_probes(SpecClient({"probes": ["junk", None]}), "x", _msgs()))
    assert [p["kind"] for p in out] == ["open", "open"]


def test_decompose_probe_returns_up_to_four_specs_and_prompt_shape() -> None:
    five = {
        "rationale": "r",
        "is_composite": True,
        "probes": [{"kind": "open", "label": f"L{i}"} for i in range(5)],
    }
    c = SpecClient(five)
    out = asyncio.run(decompose_probe(c, "technical problems", _msgs(2)))
    assert [p["label"] for p in out] == ["L0", "L1", "L2", "L3"]  # first four, in order
    system, user, schema = c.calls[0]
    assert system.strip()  # a real instruction accompanies the request
    assert "<request>technical problems</request>" in user
    # rationale is requested first to steer the split decision
    assert list(schema["schema"]["properties"])[0] == "rationale"


def test_decompose_probe_only_last_40_messages_are_sampled() -> None:
    c = SpecClient({"probes": [{"kind": "open"}]})
    asyncio.run(decompose_probe(c, "x", _msgs(100)))
    user = c.calls[0][1]
    assert "msg 99" in user
    assert "msg 60" in user
    assert "msg 59" not in user


def test_decompose_probe_empty_chat_placeholder() -> None:
    c = SpecClient({"probes": [{"kind": "open"}]})
    asyncio.run(decompose_probe(c, "x", []))
    assert "(the chat sample is empty)" in c.calls[0][1]


def test_decompose_probe_single_elementary_request() -> None:
    reply = {
        "rationale": "one dimension",
        "is_composite": False,
        "probes": [{"kind": "classification", "label": "Enjoy", "categories": ["yes", "no"]}],
    }
    out = asyncio.run(decompose_probe(SpecClient(reply), "enjoying?", _msgs()))
    assert out == [
        {
            "kind": "classification",
            "question": "",
            "categories": ["yes", "no"],
            "label": "Enjoy",
            "chart": "bars",
        }
    ]


@pytest.mark.parametrize("reply", [{"probes": "x"}, {}, [], None, {"probes": None}])
def test_decompose_probe_bad_shapes_yield_empty(reply: object) -> None:
    assert asyncio.run(decompose_probe(SpecClient(reply), "x", _msgs())) == []


def test_decompose_probe_exception_yields_empty() -> None:
    assert asyncio.run(decompose_probe(SpecClient(exc=ValueError("x")), "x", _msgs())) == []


# ---------------------------------------------------------------------------
# End to end: LLMClient behind the helpers
# ---------------------------------------------------------------------------


def test_rewrite_through_real_client_survives_format_fallback(fake_openai) -> None:
    spec_json = json.dumps(
        {"question": "q?", "categories": ["a", "b"], "label": "L", "chart": "lines"}
    )
    c = _client(
        LLMConfig(base_url="http://localhost:1234/v1", language="German"),
        script=[_HTTPError(400), _resp(spec_json)],
    )
    spec = asyncio.run(rewrite_probe_spec(c, "classification", "ask", None))
    assert spec["chart"] == "lines" and spec["categories"] == ["a", "b"]
    sent_system = _fake(c).calls[0]["messages"][0]["content"]
    assert sent_system.endswith("Write any human-readable text in German.")


def test_rewrite_through_real_client_swallows_hard_failure(fake_openai) -> None:
    c = _client(script=[_HTTPError(401)])
    spec = asyncio.run(rewrite_probe_spec(c, "open", "just this", None))
    assert spec["instruction"] == "just this"
