"""Browser-free, network-free tests for viewlyt.live pure modules.

Run: ``uv run python tests/test_live_units.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from viewlyt.live.messages import ChatMessage, message_from_ingest  # noqa: E402
from viewlyt.live.probes import (  # noqa: E402
    ClassificationProbe,
    OpenSummaryProbe,
    probe_from_dict,
)
from viewlyt.live.window import WindowBuffer, WindowConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(text: str = "hello", author: str = "user", ts: float = 1_000.0) -> ChatMessage:
    """Build a :class:`ChatMessage` directly (no ingest parsing)."""
    return ChatMessage(author=author, text=text, ts=ts)


def _clf(
    categories: list[str] | None = None,
    ema_alpha: float = 0.0,
) -> ClassificationProbe:
    cats = categories or ["happy", "angry", "neutral"]
    return ClassificationProbe(
        id="mood",
        label="Mood",
        question="How does this message feel?",
        categories=cats,
        ema_alpha=ema_alpha,
    )


def _opn() -> OpenSummaryProbe:
    return OpenSummaryProbe(
        id="summary",
        label="Summary",
        instruction="Summarize the mood in the chat.",
    )


# ---------------------------------------------------------------------------
# message_from_ingest
# ---------------------------------------------------------------------------


def test_message_from_ingest_html_preserves_emoji() -> None:
    msg = message_from_ingest({"html": '<img alt=":smile:"> hi', "ts": 1_000.0})
    assert msg is not None
    assert ":smile:" in msg.text
    assert "hi" in msg.text
    print("ok: message_from_ingest_html_preserves_emoji")


def test_message_from_ingest_plain_text() -> None:
    msg = message_from_ingest({"text": "hello"})
    assert msg is not None
    assert msg.text == "hello"
    print("ok: message_from_ingest_plain_text")


def test_message_from_ingest_blank_html_returns_none() -> None:
    result = message_from_ingest({"author": "", "html": "   "})
    assert result is None
    print("ok: message_from_ingest_blank_html_returns_none")


def test_message_from_ingest_missing_author_becomes_unknown() -> None:
    msg = message_from_ingest({"text": "no author here"})
    assert msg is not None
    assert msg.author == "unknown"
    print("ok: message_from_ingest_missing_author_becomes_unknown")


def test_message_from_ingest_ms_timestamp_folded_to_seconds() -> None:
    # 1_700_000_000_000 ms is > 1e11, must be divided by 1000
    msg = message_from_ingest({"text": "hi", "ts": 1_700_000_000_000})
    assert msg is not None
    assert msg.ts < 1e11, f"Expected seconds, got {msg.ts}"
    # sanity: should be approx 1_700_000_000 seconds
    assert abs(msg.ts - 1_700_000_000.0) < 1.0
    print("ok: message_from_ingest_ms_timestamp_folded_to_seconds")


# ---------------------------------------------------------------------------
# WindowConfig
# ---------------------------------------------------------------------------


def test_window_config_stride() -> None:
    assert WindowConfig(n=80, overlap=20).stride == 60  # 80-20
    assert WindowConfig(n=5, overlap=2).stride == 3  # 5-2
    assert WindowConfig(n=1, overlap=5).stride == 1  # max(1, …) floor
    assert WindowConfig(n=10, overlap=10).stride == 1  # overlap == n → stride 1
    print("ok: window_config_stride")


def test_window_config_from_dict_clamps() -> None:
    cfg = WindowConfig.from_dict({"n": 0, "overlap": -5})
    assert cfg.n >= 1
    assert cfg.overlap >= 0
    print("ok: window_config_from_dict_clamps")


# ---------------------------------------------------------------------------
# WindowBuffer (mode='count')
# ---------------------------------------------------------------------------


def test_window_buffer_count_mode() -> None:
    cfg = WindowConfig(n=5, overlap=2, mode="count")
    assert cfg.stride == 3

    buf = WindowBuffer()
    now = 0.0

    # 2 messages → not due yet
    buf.add(_msg("m1"))
    buf.add(_msg("m2"))
    assert not buf.due(cfg, now)

    # 3rd message → stride reached → due
    buf.add(_msg("m3"))
    assert buf.due(cfg, now)

    # emit resets the count; messages returned in order; due() is False again
    window = buf.emit(cfg, now)
    assert len(window) == 3  # min(n=5, len=3)
    assert [m.text for m in window] == ["m1", "m2", "m3"]
    assert not buf.due(cfg, now)

    # add another stride's worth → due again
    buf.add(_msg("m4"))
    buf.add(_msg("m5"))
    buf.add(_msg("m6"))
    assert buf.due(cfg, now)

    # window now has up to n=5 most-recent messages
    window2 = buf.emit(cfg, now)
    assert len(window2) == 5
    assert window2[-1].text == "m6"
    assert not buf.due(cfg, now)

    print("ok: window_buffer_count_mode")


def test_window_buffer_tail_bounds() -> None:
    buf = WindowBuffer(maxlen=10)
    for i in range(6):
        buf.add(_msg(f"m{i}"))
    assert [m.text for m in buf.tail(3)] == ["m3", "m4", "m5"]
    assert [m.text for m in buf.tail(99)] == [f"m{i}" for i in range(6)]
    assert buf.tail(0) == []
    assert buf.tail(-1) == []
    print("ok: window_buffer_tail_bounds")


def test_window_buffer_mark_emitted_resets_counters() -> None:
    cfg = WindowConfig(n=2, overlap=0, mode="count")
    buf = WindowBuffer()
    buf.add(_msg("a"))
    buf.add(_msg("b"))
    assert buf.due(cfg, 0.0)
    buf.mark_emitted(0.0)
    assert not buf.due(cfg, 0.0)
    print("ok: window_buffer_mark_emitted_resets_counters")


def test_window_buffer_sample_cleans_spam_within_margin() -> None:
    # 40 identical spam lines then 3 unique ones: the sample only cleans a bounded
    # tail (margin × n) yet still returns the n most-recent CLEANED messages.
    cfg = WindowConfig(n=3, mode="count")
    buf = WindowBuffer(maxlen=100)
    for _ in range(40):
        buf.add(_msg("REPEAT", author="spammer"))
    for i in range(3):
        buf.add(_msg(f"unique {i}", author=f"u{i}"))
    out = buf.sample(cfg)
    assert [m.text for m in out] == ["unique 0", "unique 1", "unique 2"]
    print("ok: window_buffer_sample_cleans_spam_within_margin")


def test_window_buffer_sample_respects_hygiene_toggles() -> None:
    cfg = WindowConfig(n=4, mode="count", dedupe=False, merge_authors=False)
    buf = WindowBuffer()
    for _ in range(3):
        buf.add(_msg("same", author="a"))
    buf.add(_msg("tail", author="b"))
    out = buf.sample(cfg)
    assert [m.text for m in out] == ["same", "same", "same", "tail"]
    print("ok: window_buffer_sample_respects_hygiene_toggles")


# ---------------------------------------------------------------------------
# ClassificationProbe.aggregate
# ---------------------------------------------------------------------------


def test_classification_aggregate_sums_to_100() -> None:
    probe = _clf(["happy", "angry", "neutral"])
    parsed = {
        "labels": [
            {"i": 1, "label": "happy"},
            {"i": 2, "label": "angry"},
            {"i": 3, "label": "happy"},
        ]
    }
    msgs = [_msg(f"m{i}") for i in range(3)]
    result = probe.aggregate(parsed, msgs)
    assert result.pct is not None
    assert set(result.pct.keys()) == {"happy", "angry", "neutral"}
    total = sum(result.pct.values())
    assert abs(total - 100.0) < 1.0, f"pct sum={total}"
    print("ok: classification_aggregate_sums_to_100")


def test_classification_aggregate_ignores_unknown_labels() -> None:
    probe = _clf(["happy", "angry"])
    # 'banana' is not in categories, must be ignored
    parsed = {
        "labels": [
            {"i": 1, "label": "happy"},
            {"i": 2, "label": "banana"},
            {"i": 3, "label": "happy"},
        ]
    }
    msgs = [_msg() for _ in range(3)]
    result = probe.aggregate(parsed, msgs)
    assert result.pct is not None
    # Only 'happy' and 'angry' keys; 'banana' not in pct
    assert "banana" not in result.pct
    # total of recognized labels sums to ~100 (2 happy out of 2 recognized)
    total = sum(result.pct.values())
    assert abs(total - 100.0) < 1.0
    print("ok: classification_aggregate_ignores_unknown_labels")


def test_classification_aggregate_empty_labels_gives_all_zeros() -> None:
    probe = _clf(["happy", "angry", "neutral"])
    parsed: dict = {"labels": []}
    msgs = [_msg()]
    result = probe.aggregate(parsed, msgs)
    assert result.pct is not None
    assert all(v == 0.0 for v in result.pct.values())
    print("ok: classification_aggregate_empty_labels_gives_all_zeros")


def test_classification_aggregate_tolerant_matching() -> None:
    # Models routinely vary case/accents/whitespace from the exact category text;
    # folded matching must still count them (else accented categories give all zeros).
    probe = _clf(["técnico da seleção", "fifa", "outros/nenhum"])
    parsed = {
        "labels": [
            {"i": 1, "label": "Técnico da Seleção"},  # different case
            {"i": 2, "label": "tecnico da selecao"},  # accents stripped
            {"i": 3, "label": "FIFA"},  # uppercase
            {"i": 4, "label": "  outros/nenhum "},  # extra whitespace
        ]
    }
    msgs = [_msg() for _ in range(4)]
    result = probe.aggregate(parsed, msgs)
    assert result.pct is not None
    assert sum(result.pct.values()) > 0.0, "tolerant matching must not give all zeros"
    assert result.pct["técnico da seleção"] == 50.0
    assert result.pct["fifa"] == 25.0
    assert result.pct["outros/nenhum"] == 25.0
    print("ok: classification_aggregate_tolerant_matching")


def test_classification_aggregate_flat_string_labels() -> None:
    # The optimized request returns a FLAT array of category strings (no per-item
    # {"i","label"} wrapper); aggregate counts those and still accepts legacy dicts.
    probe = _clf(["happy", "angry", "neutral"])
    msgs = [_msg() for _ in range(4)]
    flat = probe.aggregate({"labels": ["happy", "angry", "happy", "neutral"]}, msgs)
    assert flat.pct is not None
    assert flat.pct["happy"] == 50.0
    assert flat.pct["angry"] == 25.0 and flat.pct["neutral"] == 25.0
    legacy = probe.aggregate(
        {"labels": [{"i": 1, "label": "happy"}, {"i": 2, "label": "angry"}]}, msgs
    )
    assert legacy.pct is not None and legacy.pct["happy"] == 50.0
    print("ok: classification_aggregate_flat_string_labels")


def test_classification_ema_smoothing() -> None:
    cats = ["happy", "sad"]
    probe = ClassificationProbe(
        id="e",
        label="EMA",
        question="q",
        categories=cats,
        ema_alpha=0.5,
    )
    # First call seeds the EMA to the raw sample (100% happy, 0% sad)
    p1 = {"labels": [{"i": 1, "label": "happy"}, {"i": 2, "label": "happy"}]}
    msgs2 = [_msg() for _ in range(2)]
    r1 = probe.aggregate(p1, msgs2)
    assert r1.pct is not None
    first_happy = r1.pct["happy"]  # 100.0 on the first call (seeds EMA)

    # Second call: 100% sad → EMA pulls values between the two samples
    p2 = {"labels": [{"i": 1, "label": "sad"}, {"i": 2, "label": "sad"}]}
    r2 = probe.aggregate(p2, msgs2)
    assert r2.pct is not None
    second_happy = r2.pct["happy"]

    # After EMA, happy must be < first_happy (100) because new sample had 0% happy
    assert second_happy < first_happy
    # And must differ from the raw second sample (0% happy)
    assert second_happy != 0.0
    print("ok: classification_ema_smoothing")


# ---------------------------------------------------------------------------
# OpenSummaryProbe.aggregate
# ---------------------------------------------------------------------------


def test_open_summary_aggregate_returns_text() -> None:
    probe = _opn()
    parsed = {"summary": "chat is excited about the game"}
    msgs = [_msg()]
    result = probe.aggregate(parsed, msgs)
    assert result.text == "chat is excited about the game"
    print("ok: open_summary_aggregate_returns_text")


# ---------------------------------------------------------------------------
# Serialization round-trips
# ---------------------------------------------------------------------------


def test_classification_roundtrip() -> None:
    orig = _clf(["happy", "angry", "neutral"], ema_alpha=0.3)
    d = orig.to_dict()
    restored = probe_from_dict(d)
    assert restored.kind == "classification"
    assert isinstance(restored, ClassificationProbe)
    assert restored.categories == ["happy", "angry", "neutral"]
    print("ok: classification_roundtrip")


def test_open_summary_roundtrip() -> None:
    orig = _opn()
    d = orig.to_dict()
    restored = probe_from_dict(d)
    assert restored.kind == "open"
    assert isinstance(restored, OpenSummaryProbe)
    assert restored.instruction == orig.instruction
    print("ok: open_summary_roundtrip")


def test_probe_from_dict_unknown_kind_raises() -> None:
    try:
        probe_from_dict({"kind": "nope", "id": "x", "label": "y"})
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown kind")
    print("ok: probe_from_dict_unknown_kind_raises")


# ---------------------------------------------------------------------------
# Entry point for standalone execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    test_message_from_ingest_html_preserves_emoji()
    test_message_from_ingest_plain_text()
    test_message_from_ingest_blank_html_returns_none()
    test_message_from_ingest_missing_author_becomes_unknown()
    test_message_from_ingest_ms_timestamp_folded_to_seconds()
    test_window_config_stride()
    test_window_config_from_dict_clamps()
    test_window_buffer_count_mode()
    test_window_buffer_tail_bounds()
    test_window_buffer_mark_emitted_resets_counters()
    test_window_buffer_sample_cleans_spam_within_margin()
    test_window_buffer_sample_respects_hygiene_toggles()
    test_classification_aggregate_sums_to_100()
    test_classification_aggregate_ignores_unknown_labels()
    test_classification_aggregate_empty_labels_gives_all_zeros()
    test_classification_aggregate_tolerant_matching()
    test_classification_aggregate_flat_string_labels()
    test_classification_ema_smoothing()
    test_open_summary_aggregate_returns_text()
    test_classification_roundtrip()
    test_open_summary_roundtrip()
    test_probe_from_dict_unknown_kind_raises()
    print("ALL TESTS PASSED")
