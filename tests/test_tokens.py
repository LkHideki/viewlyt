"""Browser-free, tokenizer-free tests for the pure token helpers.

``split_by_budget`` takes an injectable counter, so these never import tiktoken;
we use ``len`` (1 token per char) as a deterministic fake tokenizer.
"""

from __future__ import annotations

from viewlyt.tokens import fmt_kt, split_by_budget

SEP = "\n\n"


def _ok(parts: list[str], budget: int) -> None:
    """Every part is within budget, allowing the documented separator slack."""
    for p in parts:
        assert len(p) <= budget + len(SEP), (len(p), budget, repr(p))


def test_fmt_kt() -> None:
    assert fmt_kt(0) == "0.0 kt"
    assert fmt_kt(640) == "0.6 kt"
    assert fmt_kt(12345) == "12.3 kt"
    assert fmt_kt(200_000) == "200.0 kt"


def test_no_split_when_within_budget_or_disabled() -> None:
    text = "aaaa\n\nbbbb"
    assert split_by_budget(text, 100, count=len) == [text]  # fits
    assert split_by_budget(text, 0, count=len) == [text]  # budget<=0 disables
    assert split_by_budget("", 10, count=len) == [""]  # empty


def test_packs_whole_blocks_greedily() -> None:
    text = "aaaa\n\nbbbb\n\ncccc"  # three 4-char blocks
    parts = split_by_budget(text, 10, count=len)
    assert parts == ["aaaa\n\nbbbb", "cccc"]
    _ok(parts, 10)


def test_oversized_block_hard_splits_by_lines() -> None:
    text = "aa\nbb\ncc\ndd"  # one block (no blank line), 11 chars > budget
    parts = split_by_budget(text, 6, count=len)
    assert parts == ["aa\nbb", "cc\ndd"]
    _ok(parts, 6)


def test_monster_line_splits_by_chars() -> None:
    line = "x" * 20  # single line, no way to break on \n
    parts = split_by_budget(line, 2, count=len)  # step = budget*4 = 8 chars
    assert len(parts) == 3 and "".join(parts) == line
    assert all(len(p) <= 8 for p in parts)


def test_every_block_becomes_its_own_part_when_tiny_budget() -> None:
    text = SEP.join(["one", "two", "three"])
    parts = split_by_budget(text, 5, count=len)
    assert parts == ["one", "two", "three"]
