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


if __name__ == "__main__":
    test_open_spec_builds_probe()
    test_classification_infers_categories()
    test_caller_categories_win()
    test_suggest_probes_guards_untrusted_sample()
    print("ALL TESTS PASSED")
