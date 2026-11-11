"""Regression/coverage tests for live probes, window, messages, capture, decisions.

Pure units plus fake boundaries only: a fake Selenium driver for ``BrowserCapture``,
``httpx.MockTransport`` for ``DecisionClient``. No browser, no network, no real clock.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from viewlyt.live.messages import (
    ChatMessage,
    _normalize_ts,
    clean_chat,
    drop_duplicates,
    merge_consecutive,
    message_from_ingest,
)
from viewlyt.live.probes import (
    PROBE_REGISTRY,
    ClassificationProbe,
    OpenSummaryProbe,
    Probe,
    ProbeResult,
    _auto_label,
    probe_from_dict,
)
from viewlyt.live.window import WindowBuffer, WindowConfig


def _msg(text: str, author: str = "a", ts: float = 0.0) -> ChatMessage:
    return ChatMessage(author=author, text=text, ts=ts)


def _msgs(n: int) -> list[ChatMessage]:
    return [_msg(f"m{i}", author=f"u{i}", ts=float(i)) for i in range(n)]


# ---------------------------------------------------------------------------
# probes: _auto_label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Quais são os principais problemas?", "Principais problemas"),
        ("Qual é a opinião sobre o gol", "Opinião sobre o gol"),
        ("Qual o melhor jogador", "Melhor jogador"),
        ("Como estão os fãs agora?", "Fãs agora"),
        ("Como anda o clima", "Clima"),
        ("O que estão falando do juiz?", "Estão falando do juiz"),
        ("Me diga as reclamações", "As reclamações"),
        ("Liste os memes", "Os memes"),
        ("What are the main complaints?", "Main complaints"),
        ("Which is the best moment", "Best moment"),
        ("How is the crowd feeling:", "Crowd feeling"),
        ("Summarize the chat", "Chat"),
        ("tell me the news", "News"),
        ("list top memes", "Top memes"),
        ("QUAIS SÃO OS temas", "Temas"),
        ("plain statement here!!", "Plain statement here"),
    ],
)
def test_auto_label_strips_pt_en_openers(text: str, expected: str) -> None:
    assert _auto_label(text) == expected


def test_auto_label_only_first_matching_opener_is_stripped() -> None:
    # "o que" strips once; the nested "what are the" must survive.
    assert _auto_label("O que what are the thing") == "What are the thing"


def test_auto_label_uses_first_non_blank_line_and_collapses_whitespace() -> None:
    assert _auto_label("\n\n   hello    big   world  \nsecond line") == "Hello big world"


@pytest.mark.parametrize("text", ["", "   ", "\n \n\t", "???", "?:.,;!"])
def test_auto_label_degenerate_input_falls_back_to_probe(text: str) -> None:
    assert _auto_label(text) == "probe"


def test_auto_label_caps_at_eight_words() -> None:
    assert _auto_label("a b c d e f g h i j") == "A b c d e f g h"


def test_auto_label_truncates_at_word_boundary_under_48_chars() -> None:
    words = ["alphabe", "bravoxy", "charlie", "deltaxy", "echoxyz", "foxtrot", "golfxyz", "hotelxy"]
    out = _auto_label(" ".join(words))
    # 8 words * 7 chars + 7 spaces = 63 > 48: cut to the last whole word that fits.
    assert out == "Alphabe bravoxy charlie deltaxy echoxyz foxtrot"
    assert len(out) <= 48
    assert not out.endswith(" ")


def test_auto_label_single_overlong_word_is_hard_cut() -> None:
    assert _auto_label("x" * 100) == "X" + "x" * 47


def test_auto_label_truncation_drops_trailing_punctuation() -> None:
    # 60 chars > 48. The 48-char prefix ends mid-way through "fffffffff" (right after
    # "eeeee, f"), so the partial word is dropped and the comma now trails the label:
    # the over-length branch must strip it itself.
    out = _auto_label("aaaaaaaaa bbbbbbbbb ccccccccc ddddddddd eeeee, fffffffff ggg")
    assert out == "Aaaaaaaaa bbbbbbbbb ccccccccc ddddddddd eeeee"


@pytest.mark.parametrize("punct", ["?", ":", ".", ";", "!"])
def test_auto_label_truncation_strips_each_trailing_punctuation_mark(punct: str) -> None:
    out = _auto_label(f"aaaaaaaaa bbbbbbbbb ccccccccc ddddddddd eeeee{punct} fffffffff ggg")
    assert out == "Aaaaaaaaa bbbbbbbbb ccccccccc ddddddddd eeeee"


# ---------------------------------------------------------------------------
# probes: ProbeResult / base class
# ---------------------------------------------------------------------------


def test_probe_result_to_dict_omits_unset_payloads() -> None:
    d = ProbeResult(probe_id="p", kind="classification", label="L", n=3, ts=5.0).to_dict()
    assert d == {"type": "result", "probe_id": "p", "kind": "classification", "label": "L",
                 "n": 3, "ts": 5.0}  # fmt: skip


def test_probe_result_to_dict_includes_pct_and_even_empty_text() -> None:
    d = ProbeResult(probe_id="p", kind="k", label="L", n=1, pct={"a": 100.0}, text="").to_dict()
    assert d["pct"] == {"a": 100.0}
    assert d["text"] == ""  # empty string is still a payload, only None is omitted


def test_base_probe_hooks_are_abstract() -> None:
    p = Probe("x", "X")
    with pytest.raises(NotImplementedError):
        p.build_prompt([])
    with pytest.raises(NotImplementedError):
        p.output_schema()
    with pytest.raises(NotImplementedError):
        p.aggregate({}, [])


def test_base_probe_from_dict_and_to_dict() -> None:
    p = Probe.from_dict({"id": 7})
    assert (p.id, p.label) == ("7", "7")  # label falls back to the id, stringified
    assert Probe.from_dict({"id": "a", "label": "Lbl"}).label == "Lbl"
    assert p.to_dict() == {"kind": "base", "id": "7", "label": "7"}
    p.interval_s = 12.5
    p.sample_n = 40
    assert p.to_dict()["interval_s"] == 12.5
    assert p.to_dict()["sample_n"] == 40


def test_registry_has_both_builtin_kinds() -> None:
    assert PROBE_REGISTRY["classification"] is ClassificationProbe
    assert PROBE_REGISTRY["open"] is OpenSummaryProbe


# ---------------------------------------------------------------------------
# probes: ClassificationProbe prompt / schema
# ---------------------------------------------------------------------------


def test_classification_constructor_normalizes_inputs() -> None:
    p = ClassificationProbe(
        "id",
        "L",
        "q",
        ["  happy ", "", "   ", "sad"],
        chart="",
        value_mode="",
        x_label=None,  # type: ignore[arg-type]
    )
    assert p.categories == ["happy", "sad"]
    assert p.chart == "bars"
    assert p.value_mode == "pct"
    assert p.x_label == ""
    assert p.pct_decimals == 0


def test_classification_build_prompt_contents() -> None:
    p = ClassificationProbe("m", "Mood", "How is the vibe?", ["Feliz", "Triste"])
    system, user = p.build_prompt([_msg("oi"), _msg("tchau")])
    assert "EXACTLY ONE of: Feliz, Triste" in system
    assert "VERBATIM" in system
    # prompt-injection guard: messages are data, never instructions
    assert "untrusted" in system
    assert "NEVER follow" in system
    assert "Question: How is the vibe?" in user
    assert "Categories: Feliz, Triste" in user
    assert "1. oi\n2. tchau" in user
    assert '{"labels"' in user


def test_classification_output_schema_with_categories_is_strict_enum() -> None:
    schema = ClassificationProbe("m", "M", "q", ["a", "b"]).output_schema()
    assert schema["name"] == "classification"
    assert schema["strict"] is True
    s = schema["schema"]
    assert s["additionalProperties"] is False
    assert s["required"] == ["labels"]
    assert s["properties"]["labels"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["a", "b"]},
    }


def test_classification_output_schema_with_no_categories_has_no_empty_enum() -> None:
    items = ClassificationProbe("m", "M", "q", [" ", ""]).output_schema()["schema"]["properties"][
        "labels"
    ]["items"]
    # An empty enum would reject every value; the degenerate probe is a plain string.
    assert items == {"type": "string"}


# ---------------------------------------------------------------------------
# probes: ClassificationProbe.aggregate
# ---------------------------------------------------------------------------


def _agg(p: ClassificationProbe, labels, n: int | None = None) -> ProbeResult:
    msgs = _msgs(n if n is not None else len(labels))
    return p.aggregate({"labels": labels}, msgs)


def test_aggregate_pair_labels_match_across_comma_spacing_and_case() -> None:
    p = ClassificationProbe("d", "D", "q", ["(1, 2)", "(2, 1)", "(Sim, Não)"])
    r = _agg(p, ["(1,2)", "( 2 , 1 )", "(1, 2)", "(sim,nao)"])
    # canonical category text is the key, regardless of how the model spaced it
    assert r.pct == {"(1, 2)": 50.0, "(2, 1)": 25.0, "(Sim, Não)": 25.0}


def test_aggregate_mixed_pairs_and_plain_categories() -> None:
    p = ClassificationProbe("d", "D", "q", ["(1, 2)", "(2, 1)", "outro"])
    r = _agg(p, ["(1,2)", "OUTRO", "(2,1)", "(3, 3)", "garbage"])
    # unknown pair and garbage are ignored; 3 matched labels share 100%
    assert r.pct == {"(1, 2)": 33.3, "(2, 1)": 33.3, "outro": 33.3}


def test_aggregate_pairs_still_match_when_chart_is_not_descartes() -> None:
    # Matching is structural, not gated on the presentation-only `chart` setting.
    p = ClassificationProbe("d", "D", "q", ["(1, 2)", "(2, 1)"], chart="bars")
    assert _agg(p, ["(1,2)", "(1,2)", "(2,1)"]).pct == {"(1, 2)": 66.7, "(2, 1)": 33.3}


def test_aggregate_pair_does_not_cross_match_swapped_components() -> None:
    p = ClassificationProbe("d", "D", "q", ["(1, 2)", "(2, 1)"])
    assert _agg(p, ["(2,1)"]).pct == {"(1, 2)": 0.0, "(2, 1)": 100.0}


def test_aggregate_legacy_dict_labels_and_mixed_shapes() -> None:
    p = ClassificationProbe("m", "M", "q", ["happy", "sad"])
    r = _agg(p, [{"i": 1, "label": "HAPPY"}, "sad", {"i": 3}, {"label": None}, None, 42])
    # {"i":3} and None/42 don't match; legacy dict + flat string both count
    assert r.pct == {"happy": 50.0, "sad": 50.0}
    assert r.n == 6  # n is the window size, not the matched count


@pytest.mark.parametrize("parsed", [None, {}, {"labels": None}, {"labels": []}, {"other": 1}])
def test_aggregate_missing_or_empty_labels_gives_zero_percentages(parsed) -> None:
    p = ClassificationProbe("m", "M", "q", ["a", "b"])
    r = p.aggregate(parsed, _msgs(3))
    assert r.pct == {"a": 0.0, "b": 0.0}
    assert r.n == 3
    assert (r.probe_id, r.kind, r.label) == ("m", "classification", "M")


def test_aggregate_ema_alpha_boundaries() -> None:
    # alpha == 1.0 is accepted and equals "no memory"; alpha > 1 or <= 0 disables smoothing.
    one = ClassificationProbe("m", "M", "q", ["a", "b"], ema_alpha=1.0)
    _agg(one, ["a", "a"])
    assert _agg(one, ["b", "b"]).pct == {"a": 0.0, "b": 100.0}
    for alpha in (0.0, -0.3, 1.5):
        p = ClassificationProbe("m", "M", "q", ["a", "b"], ema_alpha=alpha)
        _agg(p, ["a", "a"])
        assert _agg(p, ["b", "b"]).pct == {"a": 0.0, "b": 100.0}, alpha


def test_aggregate_ema_blends_with_previous_snapshot_and_isolates_state() -> None:
    p = ClassificationProbe("m", "M", "q", ["a", "b"], ema_alpha=0.5)
    first = _agg(p, ["a", "a"])
    assert first.pct == {"a": 100.0, "b": 0.0}  # first snapshot seeds the average
    first.pct["a"] = -1.0  # caller mutation must not corrupt the smoothing state
    second = _agg(p, ["b", "b"])
    assert second.pct == {"a": 50.0, "b": 50.0}
    third = _agg(p, ["b", "b"])
    assert third.pct == {"a": 25.0, "b": 75.0}


def test_aggregate_ema_applies_even_to_all_zero_snapshots() -> None:
    p = ClassificationProbe("m", "M", "q", ["a", "b"], ema_alpha=0.5)
    _agg(p, ["a"])
    # nothing recognised this tick: pulled halfway to zero, not reset
    assert p.aggregate({"labels": ["zzz"]}, _msgs(1)).pct == {"a": 50.0, "b": 0.0}


# ---------------------------------------------------------------------------
# probes: serialization round trips
# ---------------------------------------------------------------------------


def test_classification_to_dict_omits_empty_optionals() -> None:
    d = ClassificationProbe("m", "M", "q", ["a"]).to_dict()
    assert "colors" not in d
    assert "x_label" not in d
    assert "y_label" not in d
    assert "interval_s" not in d
    assert "sample_n" not in d
    assert d["chart"] == "bars"
    assert d["value_mode"] == "pct"
    assert d["pct_decimals"] == 0


def test_classification_full_roundtrip_with_overrides_and_colors() -> None:
    p = ClassificationProbe(
        "m",
        "Mood",
        "q?",
        ["a", "b"],
        ema_alpha=0.3,
        chart="descartes",
        colors={"a": "#fff"},
        x_label="X",
        y_label="Y",
        value_mode="count",
        pct_decimals=2,
    )
    p.interval_s = 30.0
    p.sample_n = 50
    d = p.to_dict()
    assert d["colors"] == {"a": "#fff"}
    assert d["x_label"] == "X"
    assert d["y_label"] == "Y"
    q = probe_from_dict(d)
    assert isinstance(q, ClassificationProbe)
    assert q.to_dict() == d
    assert (q.interval_s, q.sample_n) == (30.0, 50)


def test_classification_colors_are_copied_not_aliased() -> None:
    colors = {"a": "#fff"}
    p = ClassificationProbe("m", "M", "q", ["a"], colors=colors)
    colors["a"] = "#000"
    assert p.colors == {"a": "#fff"}
    d = p.to_dict()
    assert d["colors"] == {"a": "#fff"}


def test_from_dict_blank_label_derived_from_question() -> None:
    p = ClassificationProbe.from_dict(
        {"id": "m", "label": "   ", "question": "Quais são os sentimentos?", "categories": ["a"]}
    )
    assert p.label == "Sentimentos"


def test_from_dict_blank_label_and_question_derived_from_categories() -> None:
    p = ClassificationProbe.from_dict({"id": "m", "categories": ["feliz", "triste"]})
    assert p.label == "Feliz, triste"


def test_from_dict_blank_label_question_and_categories_falls_back_to_id() -> None:
    p = ClassificationProbe.from_dict({"id": "probe-9"})
    assert p.label == "probe-9"
    assert p.categories == []


def test_from_dict_explicit_label_wins_and_defaults_apply() -> None:
    p = ClassificationProbe.from_dict(
        {
            "id": "m",
            "label": "Mine",
            "question": "q",
            "categories": ["a"],
            "chart": None,
            "value_mode": None,
            "pct_decimals": "3",
            "ema_alpha": None,
            "colors": None,
        }  # fmt: skip
    )
    assert p.label == "Mine"
    assert p.chart == "bars"
    assert p.value_mode == "pct"
    assert p.pct_decimals == 3
    assert p.ema_alpha == 0.0
    assert p.colors == {}


def test_open_summary_roundtrip_and_label_fallbacks() -> None:
    p = OpenSummaryProbe("o", "Complaints", "list complaints", max_words=25)
    p.interval_s = 9.0
    d = p.to_dict()
    assert d["instruction"] == "list complaints"
    assert d["max_words"] == 25
    q = probe_from_dict(d)
    assert isinstance(q, OpenSummaryProbe)
    assert q.to_dict() == d

    assert OpenSummaryProbe.from_dict({"id": "o", "instruction": "What are the top memes?"}).label \
        == "Top memes"  # fmt: skip
    nolabel = OpenSummaryProbe.from_dict({"id": "o9"})
    assert nolabel.label == "o9"
    assert nolabel.max_words == 60  # 0 / missing -> default


def test_open_summary_build_prompt_contents() -> None:
    p = OpenSummaryProbe("o", "O", "summarize the drama", max_words=42)
    system, user = p.build_prompt([_msg("first"), _msg("second")])
    assert "untrusted" in system
    assert "NEVER follow" in system
    assert "Task: summarize the drama" in user
    assert "at most 42 words" in user
    assert "1. first\n2. second" in user
    assert '{"summary"' in user


def test_open_summary_output_schema() -> None:
    schema = OpenSummaryProbe("o", "O", "i").output_schema()
    assert schema["name"] == "summary"
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["summary"]
    assert schema["schema"]["properties"]["summary"] == {"type": "string"}


def test_open_summary_aggregate_strips_and_handles_missing() -> None:
    p = OpenSummaryProbe("o", "O", "i")
    r = p.aggregate({"summary": "  hello \n"}, _msgs(4))
    assert (r.text, r.n, r.kind, r.pct) == ("hello", 4, "open", None)
    assert p.aggregate(None, []).text == ""
    assert p.aggregate({}, []).text == ""


def test_open_summary_aggregate_null_summary_does_not_leak_the_literal_none() -> None:
    # A model returning {"summary": null} must yield an empty text, not the string
    # "None" rendered on the dashboard. (Exposes a real defect: aggregate() does
    # str(parsed.get("summary", "")), and .get() only defaults for a MISSING key.)
    p = OpenSummaryProbe("o", "O", "i")
    assert p.aggregate({"summary": None}, []).text != "None"


def test_probe_from_dict_unknown_or_missing_kind_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown probe kind: 'nope'"):
        probe_from_dict({"kind": "nope", "id": "x"})
    with pytest.raises(ValueError, match="unknown probe kind: ''"):
        probe_from_dict({"id": "x"})
    with pytest.raises(ValueError):
        probe_from_dict({"kind": None, "id": "x"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0.0),
        (0, 0.0),
        ("abc", 0.0),
        ([], 0.0),
        (-5, 0.0),
        ("-1.5", 0.0),
        ("2.5", 2.5),
        (12, 12.0),
    ],
)
def test_probe_from_dict_interval_override_parsing(raw, expected) -> None:
    p = probe_from_dict({"kind": "open", "id": "o", "instruction": "i", "interval_s": raw})
    assert p.interval_s == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), (0, 0), ("abc", 0), ({}, 0), (-3, 0), ("-7", 0), ("7", 7), (12, 12), (3.9, 3)],
)
def test_probe_from_dict_sample_n_override_parsing(raw, expected) -> None:
    p = probe_from_dict({"kind": "open", "id": "o", "instruction": "i", "sample_n": raw})
    assert p.sample_n == expected


# ---------------------------------------------------------------------------
# window
# ---------------------------------------------------------------------------


def _filled(k: int) -> WindowBuffer:
    buf = WindowBuffer()
    for m in _msgs(k):
        buf.add(m)
    return buf


@pytest.mark.parametrize("mode", ["count", "time", "hybrid", "bogus"])
def test_due_is_false_for_an_empty_buffer_in_every_mode(mode: str) -> None:
    cfg = WindowConfig(n=1, gap=0.0, mode=mode)
    assert not WindowBuffer().due(cfg, 1e9)


def test_due_count_mode_follows_stride_and_ignores_time() -> None:
    cfg = WindowConfig(n=4, overlap=2, gap=0.0, mode="count")  # stride 2
    buf = WindowBuffer()
    buf.add(_msg("1"))
    # gap=0 and never emitted would make time "due", but count mode must ignore it
    assert not buf.due(cfg, 999.0)
    buf.add(_msg("2"))
    assert buf.due(cfg, 999.0)
    buf.mark_emitted(0.0)
    assert not buf.due(cfg, 10_000.0)  # even far in the future: no new messages
    buf.add(_msg("3"))
    assert not buf.due(cfg, 10_000.0)
    buf.add(_msg("4"))
    assert buf.due(cfg, 10_000.0)


def test_due_unknown_mode_behaves_like_count() -> None:
    cfg = WindowConfig(n=3, gap=0.0, mode="whatever")
    buf = _filled(2)
    assert not buf.due(cfg, 1e6)
    buf.add(_msg("x"))
    assert buf.due(cfg, 1e6)


def test_due_time_mode_first_emit_then_gap_boundary() -> None:
    cfg = WindowConfig(n=1000, gap=45.0, mode="time")
    buf = _filled(1)
    assert buf.due(cfg, 0.0)  # nothing emitted yet -> time is due immediately
    buf.mark_emitted(100.0)
    buf.add(_msg("more"))
    assert not buf.due(cfg, 144.9)
    assert buf.due(cfg, 145.0)  # >= gap
    assert buf.due(cfg, 500.0)


def test_due_time_mode_ignores_message_count() -> None:
    cfg = WindowConfig(n=2, gap=60.0, mode="time")
    buf = _filled(1)
    buf.mark_emitted(0.0)
    for m in _msgs(50):
        buf.add(m)  # way past the count stride
    assert not buf.due(cfg, 30.0)


def test_due_time_mode_with_empty_buffer_after_emit_still_false() -> None:
    buf = WindowBuffer()
    buf.mark_emitted(0.0)
    assert not buf.due(WindowConfig(mode="time", gap=1.0), 100.0)


def test_due_hybrid_fires_on_whichever_comes_first() -> None:
    cfg = WindowConfig(n=3, gap=45.0, mode="hybrid")
    buf = _filled(1)
    buf.mark_emitted(0.0)
    buf.add(_msg("b"))
    assert not buf.due(cfg, 10.0)  # neither count (1 < 3) nor time (10 < 45)
    buf.add(_msg("c"))
    buf.add(_msg("d"))
    assert buf.due(cfg, 10.0)  # count-driven
    buf.mark_emitted(10.0)
    buf.add(_msg("e"))
    assert not buf.due(cfg, 50.0)
    assert buf.due(cfg, 55.0)  # time-driven with a single new message


def test_buffer_maxlen_evicts_oldest_but_stride_counts_all_adds() -> None:
    buf = WindowBuffer(maxlen=5)
    for m in _msgs(12):
        buf.add(m)
    assert len(buf) == 5
    assert [m.text for m in buf.tail(10)] == ["m7", "m8", "m9", "m10", "m11"]
    # the stride counter tracks arrivals, not what survived eviction
    assert buf.due(WindowConfig(n=12, mode="count"), 0.0)


def test_tail_returns_a_copy_and_handles_non_positive_k() -> None:
    buf = _filled(3)
    assert buf.tail(0) == []
    assert buf.tail(-4) == []
    full = buf.tail(99)
    full.clear()
    assert len(buf) == 3  # mutating the returned list doesn't touch the buffer
    assert [m.text for m in buf.tail(2)] == ["m1", "m2"]  # order preserved


def test_sample_returns_last_n_after_hygiene() -> None:
    buf = WindowBuffer()
    for t in ["a", "b", "c", "d", "e"]:
        buf.add(_msg(t, author=t))
    out = buf.sample(WindowConfig(n=3))
    assert [m.text for m in out] == ["c", "d", "e"]


def test_sample_dedupe_and_merge_toggles_independently() -> None:
    buf = WindowBuffer()
    for author, text in [("a", "hi"), ("a", "hi!"), ("a", "yo"), ("b", "sup")]:
        buf.add(_msg(text, author=author))
    both = buf.sample(WindowConfig(n=10))
    assert [(m.author, m.text) for m in both] == [("a", "hi / yo"), ("b", "sup")]
    only_dedupe = buf.sample(WindowConfig(n=10, merge_authors=False))
    assert [m.text for m in only_dedupe] == ["hi", "yo", "sup"]
    only_merge = buf.sample(WindowConfig(n=10, dedupe=False))
    assert [m.text for m in only_merge] == ["hi / hi! / yo", "sup"]
    raw = buf.sample(WindowConfig(n=10, dedupe=False, merge_authors=False))
    assert [m.text for m in raw] == ["hi", "hi!", "yo", "sup"]


def test_sample_does_not_mutate_buffered_messages() -> None:
    buf = WindowBuffer()
    buf.add(_msg("one", author="a"))
    buf.add(_msg("two", author="a"))
    buf.sample(WindowConfig(n=10))
    assert [m.text for m in buf.tail(10)] == ["one", "two"]


def test_sample_cleans_only_a_bounded_tail_and_can_come_up_short_under_spam() -> None:
    buf = WindowBuffer()
    for i in range(30):
        buf.add(_msg(f"unique {i}", author=f"u{i}"))
    for _ in range(8):
        buf.add(_msg("SPAM", author="spammer"))
    # n=2 -> raw tail of 8, all one author's identical line -> collapses to one message
    out = buf.sample(WindowConfig(n=2))
    assert [m.text for m in out] == ["SPAM"]


def test_sample_still_reaches_n_when_three_quarters_of_the_raw_tail_folds_away() -> None:
    # n=2 -> the raw tail is 4n = 8 messages: 2 unique authors then 6 identical spam
    # lines (75% folds away). The tail must be wide enough that the two survivors
    # (the last unique author and the collapsed spam) still fill the window; a
    # narrower tail would see only spam and come up short.
    buf = WindowBuffer()
    buf.add(_msg("early", author="early"))
    buf.add(_msg("u0", author="u0"))
    buf.add(_msg("u1", author="u1"))
    for _ in range(6):
        buf.add(_msg("SPAM", author="spammer"))
    out = buf.sample(WindowConfig(n=2))
    assert [(m.author, m.text) for m in out] == [("u1", "u1"), ("spammer", "SPAM")]


def test_sample_with_nonpositive_n_is_treated_as_one() -> None:
    buf = _filled(5)
    assert [m.text for m in buf.sample(WindowConfig(n=0))] == ["m4"]


def test_window_config_capacity_clamped_and_roundtrip() -> None:
    cfg = WindowConfig.from_dict({"capacity": 5, "n": 0, "overlap": -4, "gap": -9})
    assert (cfg.capacity, cfg.n, cfg.overlap, cfg.gap) == (100, 1, 0, 0.0)
    rt = WindowConfig.from_dict(WindowConfig(n=50, overlap=5, gap=7.5, mode="time",
                                             capacity=500, dedupe=False,
                                             merge_authors=False).to_dict())  # fmt: skip
    assert rt == WindowConfig(n=50, overlap=5, gap=7.5, mode="time", capacity=500,
                              dedupe=False, merge_authors=False)  # fmt: skip


def test_window_config_from_empty_dict_yields_defaults() -> None:
    assert WindowConfig.from_dict({}) == WindowConfig()


def test_window_config_stride_floor_and_negative_overlap() -> None:
    assert WindowConfig(n=5, overlap=99).stride == 1
    assert WindowConfig(n=5, overlap=-3).stride == 5


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.0),
        ("abc", 0.0),
        ([], 0.0),
        ({}, 0.0),
        (1_700_000_000, 1_700_000_000.0),
        ("1700000000", 1_700_000_000.0),
        (1_700_000_000_000, 1_700_000_000.0),
        ("1700000000000", 1_700_000_000.0),
        (1_700_000_000.5, 1_700_000_000.5),
        (0, 0.0),
        (-5, -5.0),
        (1e11, 1e11),  # boundary is exclusive: exactly 1e11 is treated as seconds
        (1e11 + 1, (1e11 + 1) / 1000.0),
    ],
)
def test_normalize_ts(value, expected: float) -> None:
    assert _normalize_ts(value) == expected


@pytest.mark.parametrize("payload", [None, [], "text", 5, ("html", "x")])
def test_message_from_ingest_rejects_non_dict_payloads(payload) -> None:
    assert message_from_ingest(payload) is None  # type: ignore[arg-type]


def test_message_from_ingest_empty_payloads_are_dropped() -> None:
    assert message_from_ingest({}) is None
    assert message_from_ingest({"text": ""}) is None
    assert message_from_ingest({"text": None, "html": None}) is None
    assert message_from_ingest({"html": "<b> </b>"}) is None


def test_message_from_ingest_html_wins_over_text_when_present() -> None:
    msg = message_from_ingest({"html": "<b>from html</b>", "text": "from text"})
    assert msg is not None
    assert msg.text == "from html"


def test_message_from_ingest_flattens_and_normalizes_fields() -> None:
    msg = message_from_ingest(
        {"html": "line one<br>line   two &amp; more", "author": "  Ann  ", "id": 123, "ts": "5"}
    )
    assert msg is not None
    assert "\n" not in msg.text
    assert "&amp;" not in msg.text
    assert "& more" in msg.text
    assert (msg.author, msg.id, msg.ts) == ("Ann", "123", 5.0)


@pytest.mark.parametrize("author", [None, "", "   ", 0])
def test_message_from_ingest_falsy_author_becomes_unknown(author) -> None:
    msg = message_from_ingest({"text": "hi", "author": author})
    assert msg is not None
    assert msg.author == "unknown"


def test_message_from_ingest_missing_id_is_empty_string() -> None:
    msg = message_from_ingest({"text": "hi"})
    assert msg is not None
    assert msg.id == ""
    assert msg.ts == 0.0


def test_drop_duplicates_is_per_author_and_punctuation_insensitive() -> None:
    msgs = [
        _msg("Loooool!!", "a"),
        _msg("lool", "a"),  # run capped to "lool", punctuation stripped, casefolded
        _msg("LOOL", "b"),  # different author: survives
        _msg("lool.", "b"),  # b's duplicate
    ]
    out = drop_duplicates(msgs)
    assert [(m.author, m.text) for m in out] == [("a", "Loooool!!"), ("b", "LOOL")]
    assert len(msgs) == 4  # input untouched


def test_drop_duplicates_caps_a_run_of_exactly_three_characters() -> None:
    # "looo" has a run of exactly 3 'o': capped to 2 it equals "loo", so it is a dup.
    out = drop_duplicates([_msg("looo", "a"), _msg("loo", "a"), _msg("lo", "a")])
    assert [m.text for m in out] == ["looo", "lo"]


def test_drop_duplicates_keeps_emoji_only_and_punctuation_only_repeats() -> None:
    msgs = [_msg("😀", "a"), _msg("😀", "a"), _msg("!!!", "a"), _msg("!!!", "a")]
    assert len(drop_duplicates(msgs)) == 4  # normalized text is empty -> never a duplicate


def test_merge_consecutive_joins_only_adjacent_same_author_runs() -> None:
    msgs = [_msg("1", "a", 1.0), _msg("2", "a", 2.0), _msg("3", "b"), _msg("4", "a")]
    out = merge_consecutive(msgs)
    assert [(m.author, m.text) for m in out] == [("a", "1 / 2"), ("b", "3"), ("a", "4")]
    assert out[0].ts == 1.0  # head's ts/id are kept
    assert [m.text for m in msgs] == ["1", "2", "3", "4"]  # input not mutated


@pytest.mark.parametrize("anon", ["unknown", "UNKNOWN", "Unknown", ""])
def test_merge_consecutive_never_merges_anonymous_authors(anon: str) -> None:
    out = merge_consecutive([_msg("x", anon), _msg("y", anon)])
    assert [m.text for m in out] == ["x", "y"]


def test_merge_consecutive_empty_input() -> None:
    assert merge_consecutive([]) == []


def test_clean_chat_dedupes_before_merging_and_respects_flags() -> None:
    msgs = [_msg("hi", "a"), _msg("hi!", "a"), _msg("yo", "a"), _msg("hey", "b")]
    assert [m.text for m in clean_chat(msgs)] == ["hi / yo", "hey"]
    assert [m.text for m in clean_chat(msgs, merge_authors=False)] == ["hi", "yo", "hey"]
    assert [m.text for m in clean_chat(msgs, dedupe=False)] == ["hi / hi! / yo", "hey"]
    assert [m.text for m in clean_chat(msgs, dedupe=False, merge_authors=False)] == [
        "hi", "hi!", "yo", "hey",
    ]  # fmt: skip
    assert clean_chat([]) == []


# ---------------------------------------------------------------------------
# capture (fake Selenium driver)
# ---------------------------------------------------------------------------

SNIPPET = "window.__viewlyt_running = true; /* cov snippet */"
ALIVE_CHECK = "return !!window.__viewlyt_running"


def _capture_deps():
    pytest.importorskip("selenium")
    from selenium.common.exceptions import WebDriverException

    from viewlyt.live import capture

    return capture, WebDriverException


def _fake_driver_cls():
    capture, WebDriverException = _capture_deps()

    class FakeDriver:
        def __init__(self) -> None:
            self.gets: list[str] = []
            self.scripts: list[str] = []
            self.quit_calls = 0
            self.quit_raises = False
            self.alive_raises = False
            self.alive_results: list[object] = []

        def get(self, url: str) -> None:
            self.gets.append(url)

        def add_cookie(self, cookie: dict) -> None:
            pass

        def find_element(self, *a: object, **k: object) -> None:
            raise WebDriverException("no consent dialog in the fake")

        def execute_script(self, script: str, *a: object) -> object:
            self.scripts.append(script)
            if script == ALIVE_CHECK:
                if self.alive_raises:
                    raise WebDriverException("session died")
                return self.alive_results.pop(0) if self.alive_results else True
            return None

        def quit(self) -> None:
            self.quit_calls += 1
            if self.quit_raises:
                raise WebDriverException("already dead")

    return capture, FakeDriver


def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.005)


def test_capture_default_factory_builds_headless_chrome_with_pna_flags(monkeypatch) -> None:
    capture, FakeDriver = _fake_driver_cls()
    import viewlyt.driver as driver_mod

    calls: list[dict] = []
    fake = FakeDriver()

    def fake_build_driver(**kwargs):
        calls.append(kwargs)
        return fake

    monkeypatch.setattr(driver_mod, "build_driver", fake_build_driver)
    cap = capture.BrowserCapture(capture.popout_url("dQw4w9WgXcQ"), SNIPPET)  # no factory
    cap.start()
    _wait_for(lambda: SNIPPET in fake.scripts)
    cap.stop()

    assert len(calls) == 1
    assert calls[0]["headless"] is True
    extra = calls[0]["extra_args"]
    assert tuple(extra) == capture._PNA_DISABLE_FLAGS
    joined = " ".join(extra)
    # the flags that let the https popout reach the loopback ws:// (headless can't prompt)
    assert "LocalNetworkAccessChecks" in joined
    assert "PrivateNetworkAccessChecks" in joined
    assert fake.quit_calls == 1


def test_capture_stop_swallows_driver_quit_errors() -> None:
    capture, FakeDriver = _fake_driver_cls()
    driver = FakeDriver()
    driver.quit_raises = True
    cap = capture.BrowserCapture("https://x", SNIPPET, driver_factory=lambda: driver)
    cap.start()
    _wait_for(lambda: SNIPPET in driver.scripts)
    escaped: list[threading.ExceptHookArgs] = []
    original_hook = threading.excepthook
    threading.excepthook = escaped.append  # a raise on the capture thread lands here
    try:
        cap.stop()  # must not raise
    finally:
        threading.excepthook = original_hook
    assert driver.quit_calls == 1
    assert not cap._thread.is_alive()
    assert escaped == []  # quit() failure was swallowed, not leaked out of the thread


def test_capture_rebuilds_and_quits_old_driver_when_health_check_raises() -> None:
    capture, FakeDriver = _fake_driver_cls()
    first, second = FakeDriver(), FakeDriver()
    first.alive_raises = True
    first.quit_raises = True  # a dead session's quit() failing must not kill the loop
    drivers = [first, second]
    cap = capture.BrowserCapture(
        "https://x",
        SNIPPET,
        driver_factory=lambda: drivers.pop(0),
        health_interval=0.005,
        retry_backoff=0.005,
    )
    cap.start()
    _wait_for(lambda: SNIPPET in second.scripts)
    cap.stop()
    assert first.quit_calls == 1  # old browser released before rebuilding
    assert second.quit_calls == 1


def test_capture_falsy_health_result_triggers_reinject_but_truthy_does_not() -> None:
    capture, FakeDriver = _fake_driver_cls()
    driver = FakeDriver()
    driver.alive_results = [None, 0, True, True]
    cap = capture.BrowserCapture(
        "https://x", SNIPPET, driver_factory=lambda: driver, health_interval=0.005
    )
    cap.start()
    _wait_for(lambda: driver.scripts.count(ALIVE_CHECK) >= 5)
    cap.stop()
    # initial inject + one re-inject for None + one for 0 (and none for True)
    assert driver.scripts.count(SNIPPET) == 3


def test_capture_stop_interrupts_a_long_retry_backoff() -> None:
    capture, _ = _fake_driver_cls()
    attempts = 0

    def always_fails():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("no chrome")

    cap = capture.BrowserCapture(
        "https://x", SNIPPET, driver_factory=always_fails, retry_backoff=60.0
    )
    cap.start()
    _wait_for(lambda: attempts >= 1)
    cap.stop(timeout=2.0)
    # joined within the timeout: the stop event woke it from the 60s backoff
    assert not cap._thread.is_alive()
    assert attempts == 1


def test_capture_stop_before_start_is_a_noop() -> None:
    capture, _ = _fake_driver_cls()
    cap = capture.BrowserCapture("https://x", SNIPPET, driver_factory=lambda: None)
    cap.stop()  # thread never started: must not raise or block
    assert not cap._thread.is_alive()


def test_unstopped_capture_does_not_keep_the_interpreter_alive() -> None:
    # Behavioral daemon check: a process that starts a capture whose driver factory
    # blocks forever and never calls stop() must still exit promptly.
    import subprocess
    import sys

    code = (
        "import threading\n"
        "from viewlyt.live.capture import BrowserCapture\n"
        "ev = threading.Event()\n"
        "cap = BrowserCapture('https://x', 'snippet', driver_factory=lambda: ev.wait())\n"
        "cap.start()\n"
        "print('started')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "started"


def test_popout_url_extracts_id_from_various_shapes() -> None:
    capture, _ = _capture_deps()
    expected = "https://www.youtube.com/live_chat?is_popout=1&v=dQw4w9WgXcQ"
    assert capture.popout_url("https://youtu.be/dQw4w9WgXcQ") == expected
    assert capture.popout_url("https://www.youtube.com/live/dQw4w9WgXcQ") == expected


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["http://[::1", "https://[bad", "http://[::1]x:80/"])
def test_decisions_url_malformed_base_url_is_none_not_an_exception(url: str) -> None:
    from viewlyt.live.decisions import decisions_url

    assert decisions_url(url) is None


def test_decisions_url_rejects_lookalike_hosts() -> None:
    from viewlyt.live.decisions import decisions_url

    for url in (
        "https://evilopenrouter.ai/v1",
        "https://openrouter.ai.corp-gw.example/v1",
        "https://api.perplexity.ai.evil.example/v1",
        "https://notapi.perplexity.ai/v1",
    ):
        assert decisions_url(url) is None, url


def test_decision_client_rejects_unsupported_base_url() -> None:
    pytest.importorskip("httpx")
    from viewlyt.live.decisions import DecisionClient

    with pytest.raises(ValueError, match="no decisions endpoint"):
        DecisionClient("https://api.openai.com/v1", "k", "m")


def test_decision_client_aclose_closes_the_http_client() -> None:
    pytest.importorskip("httpx")
    import httpx

    from viewlyt.live.decisions import DecisionClient

    def handler(request: httpx.Request) -> httpx.Response:
        qs = json_questions(request)
        return httpx.Response(
            200,
            json={"answers": {q: {"type": "choice", "choice": "a"} for q in qs}},
        )

    def json_questions(request: httpx.Request) -> list[str]:
        import json

        return list(json.loads(request.content)["questions"])

    client = DecisionClient(
        "https://api.perplexity.ai", "k", "pplx-decider-flash-v1.2",
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    probe = ClassificationProbe("p", "P", "q", ["a", "b"])

    async def scenario():
        out = await client.classify(probe, _msgs(2))
        await client.aclose()
        with pytest.raises(RuntimeError):  # httpx refuses requests on a closed client
            await client.classify(probe, _msgs(2))
        return out

    out = asyncio.run(scenario())
    assert out == {"labels": ["a", "a"]}
    assert client.avg_latency_ms is not None
    assert client.total_cost == 0.0  # no usage block in the mock -> nothing to price
