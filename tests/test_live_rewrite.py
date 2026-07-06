"""Browser-free, network-free tests for the R1 probe-rewrite path.

Exercises :func:`viewlyt.live.llm.rewrite_probe_spec` against a fake client (no
``openai`` import, no network) and checks that the produced specs reconstruct
into valid probes via :func:`probe_from_dict`.

Run: ``uv run python tests/test_live_rewrite.py`` or ``uv run pytest tests/test_live_rewrite.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewlyt.live.llm import rewrite_probe_spec  # noqa: E402
from viewlyt.live.probes import probe_from_dict  # noqa: E402


class FakeRewriteClient:
    """Stub LLMRunner: no network, returns canned specs based on the schema shape."""

    model = "x"

    async def run(self, probe, messages) -> dict:  # noqa: ARG002 - no-op stub
        return {}

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:  # noqa: ARG002
        props = schema["schema"]["properties"]
        if "instruction" in props:
            return {
                "instruction": "Across all the sampled messages, summarize the audience mood.",
                "label": "Mood",
                "max_words": 60,
            }
        return {
            "question": "Classify each message's sentiment.",
            "categories": ["happy", "angry", "neutral"],
            "label": "Sentiment",
            "chart": "bars",
        }


def test_open_spec_builds_probe() -> None:
    spec = asyncio.run(rewrite_probe_spec(FakeRewriteClient(), "open", "how do they feel?", None))
    probe = probe_from_dict({"kind": "open", "id": "x", **spec})
    assert probe.kind == "open"
    assert probe.instruction
    assert probe.label
    print("ok: open_spec_builds_probe")


def test_classification_infers_categories() -> None:
    spec = asyncio.run(
        rewrite_probe_spec(FakeRewriteClient(), "classification", "how do they feel?", None)
    )
    probe = probe_from_dict({"kind": "classification", "id": "y", **spec})
    assert len(probe.categories) >= 2
    assert probe.chart in ("bars", "columns", "stacked", "donut", "lines", "area", "delta")
    print("ok: classification_infers_categories")


def test_caller_categories_win() -> None:
    spec = asyncio.run(
        rewrite_probe_spec(FakeRewriteClient(), "classification", "sort them", ["yes", "no"])
    )
    assert spec["categories"] == ["yes", "no"]
    print("ok: caller_categories_win")


class _CapturingClient:
    """Captures the (system, user) prompt suggest_probes builds; returns two valid probes."""

    model = "x"
    system = ""
    user = ""

    async def run(self, probe, messages) -> dict:  # noqa: ARG002 - no-op stub
        return {}

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:  # noqa: ARG002
        self.system, self.user = system, user
        return {
            "probes": [
                {
                    "kind": "open",
                    "instruction": "Across all the sampled messages, gauge the mood.",
                    "label": "Mood",
                    "max_words": 40,
                },
                {
                    "kind": "classification",
                    "question": "Classify each message.",
                    "categories": ["a", "b", "c"],
                    "label": "Kind",
                    "chart": "bars",
                },
            ]
        }


def test_suggest_probes_guards_untrusted_sample() -> None:
    # secperf S9: suggest_probes must carry the same untrusted-content guard as its
    # sibling decompose_probe — a warning in the system prompt AND the sample wrapped
    # in a delimiter, so injected chat can't steer the proposed probes.
    from viewlyt.live.llm import suggest_probes

    client = _CapturingClient()
    asyncio.run(suggest_probes(client, "what's the mood?", []))
    assert "untrusted" in client.system.lower()
    assert "never" in client.system.lower()
    assert "<chat_sample>" in client.user
    assert "<request>" in client.user
    print("ok: suggest_probes_guards_untrusted_sample")


# ---------------------------------------------------------------------------
# LLMClient._complete: fail-fast on non-format errors (no 3× timeout hang).
# ---------------------------------------------------------------------------


class _RaisingCompletions:
    """Fake ``chat.completions`` whose ``create`` always raises, counting calls."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def create(self, **_kw: object) -> object:
        self.calls += 1
        raise self.exc


def _bare_client(exc: Exception):
    """An LLMClient wired to a raising fake, WITHOUT running __init__ (no openai)."""
    from viewlyt.live.llm import LLMClient

    comp = _RaisingCompletions(exc)
    client = object.__new__(LLMClient)
    client._client = type("_C", (), {"chat": type("_Ch", (), {"completions": comp})()})()
    client.model = "m"
    client._usage_extra = {}
    client._rf_mode = None
    client._latency_count = 0
    client._latency_sum_ms = 0.0
    return client, comp


def test_complete_fails_fast_on_timeout() -> None:
    # A timeout carries no .status_code, so _complete must NOT retry the other two
    # response_format modes — otherwise a dead endpoint hangs for 3× the timeout.
    client, comp = _bare_client(TimeoutError("endpoint not responding"))
    try:
        asyncio.run(client._complete([{"role": "user", "content": "x"}], {"name": "s"}))
        raise AssertionError("expected the timeout to propagate")
    except TimeoutError:
        pass
    assert comp.calls == 1, f"expected 1 attempt, got {comp.calls}"
    print("ok: complete_fails_fast_on_timeout")


def test_complete_falls_through_on_format_rejection() -> None:
    # A 400 means the provider rejected this response_format, so _complete SHOULD
    # fall through to json_object then plain — three attempts before giving up.
    err = Exception("bad response_format")
    err.status_code = 400  # type: ignore[attr-defined]
    client, comp = _bare_client(err)
    try:
        asyncio.run(client._complete([{"role": "user", "content": "x"}], {"name": "s"}))
        raise AssertionError("expected the error to propagate after all modes")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 400
    assert comp.calls == 3, f"expected 3 attempts, got {comp.calls}"
    print("ok: complete_falls_through_on_format_rejection")


if __name__ == "__main__":
    test_open_spec_builds_probe()
    test_classification_infers_categories()
    test_caller_categories_win()
    test_suggest_probes_guards_untrusted_sample()
    test_complete_fails_fast_on_timeout()
    test_complete_falls_through_on_format_rejection()
    print("ALL TESTS PASSED")
