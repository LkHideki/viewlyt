"""Browser-free, network-free integration test for viewlyt.live.llm.run_probes.

Uses a fake LLM client to drive the full probe pipeline without any real network
call or FastAPI import.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewlyt.live.llm import run_probes  # noqa: E402
from viewlyt.live.messages import ChatMessage  # noqa: E402
from viewlyt.live.probes import ClassificationProbe, OpenSummaryProbe, Probe  # noqa: E402

# ---------------------------------------------------------------------------
# Fake LLM client
# ---------------------------------------------------------------------------


class FakeLLMClient:
    """Zero-network stand-in for :class:`~viewlyt.live.llm.LLMClient`.

    For classification: assigns each message (by index) a label cycling through
    the probe's categories, so every category is represented at least once when
    ``len(messages) >= len(categories)``.
    For open: returns a short summary that encodes the message count.
    """

    model = "fake"

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        if probe.kind == "classification":
            cats = probe.categories  # type: ignore[attr-defined]
            return {
                "labels": [{"i": i + 1, "label": cats[i % len(cats)]} for i in range(len(messages))]
            }
        if probe.kind == "open":
            return {"summary": f"ok: {len(messages)} msgs"}
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msgs(n: int = 6) -> list[ChatMessage]:
    return [ChatMessage(author=f"user{i}", text=f"message {i}", ts=float(i)) for i in range(n)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_probes_returns_two_results() -> None:
    clf = ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does this feel?",
        categories=["happy", "angry", "neutral"],
    )
    opn = OpenSummaryProbe(
        id="summary",
        label="Summary",
        instruction="Summarize the chat.",
    )
    msgs = _msgs(6)

    results = asyncio.run(run_probes(FakeLLMClient(), [clf, opn], msgs))

    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    print("ok: run_probes_returns_two_results")


def test_classification_result_pct_keys_and_sum() -> None:
    cats = ["happy", "angry", "neutral"]
    clf = ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does this feel?",
        categories=cats,
    )
    opn = OpenSummaryProbe(
        id="summary",
        label="Summary",
        instruction="Summarize the chat.",
    )
    msgs = _msgs(6)  # 6 messages, 3 cats → 2 of each

    results = asyncio.run(run_probes(FakeLLMClient(), [clf, opn], msgs))

    clf_result = next(r for r in results if r.kind == "classification")
    assert clf_result.pct is not None
    # Exactly the 3 category keys
    assert set(clf_result.pct.keys()) == set(cats)
    # Percentages sum to ~100 (allow small rounding)
    total = sum(clf_result.pct.values())
    assert abs(total - 100.0) < 1.0, f"pct total={total}"
    print("ok: classification_result_pct_keys_and_sum")


def test_open_result_text_starts_with_ok() -> None:
    clf = ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does this feel?",
        categories=["happy", "angry", "neutral"],
    )
    opn = OpenSummaryProbe(
        id="summary",
        label="Summary",
        instruction="Summarize the chat.",
    )
    msgs = _msgs(6)

    results = asyncio.run(run_probes(FakeLLMClient(), [clf, opn], msgs))

    opn_result = next(r for r in results if r.kind == "open")
    assert opn_result.text is not None
    assert opn_result.text.startswith("ok:"), f"unexpected text: {opn_result.text!r}"
    print("ok: open_result_text_starts_with_ok")
