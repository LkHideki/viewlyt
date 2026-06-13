"""Pure tests for the live-chat spam hygiene (dedupe + same-author merge).

Browser-free, stdlib-only. Also runnable standalone:

    python tests/test_live_clean.py
"""

from __future__ import annotations

from viewlyt.live.messages import (
    ChatMessage,
    clean_chat,
    drop_duplicates,
    merge_consecutive,
)
from viewlyt.live.window import WindowConfig


def _m(author: str, text: str, ts: float = 0.0) -> ChatMessage:
    return ChatMessage(author=author, text=text, ts=ts)


def test_drop_duplicates_collapses_one_users_spam() -> None:
    msgs = [_m("spammer", "LErooo!!!"), _m("spammer", "leroo"), _m("spammer", "LEROO")]
    out = drop_duplicates(msgs)
    assert len(out) == 1
    assert out[0].text == "LErooo!!!"  # first occurrence kept verbatim


def test_drop_duplicates_keeps_different_authors() -> None:
    out = drop_duplicates([_m("a", "lol"), _m("b", "lol")])
    assert len(out) == 2


def test_drop_duplicates_keeps_distinct_text() -> None:
    out = drop_duplicates([_m("a", "hi"), _m("a", "bye"), _m("a", "hi")])
    assert [m.text for m in out] == ["hi", "bye"]


def test_drop_duplicates_keeps_symbol_only() -> None:
    # Normalized text is empty, so it's never treated as a duplicate.
    out = drop_duplicates([_m("a", "!!!"), _m("a", "!!!")])
    assert len(out) == 2


def test_merge_consecutive_same_author() -> None:
    msgs = [_m("u", "I think"), _m("u", "the stream"), _m("u", "is great")]
    out = merge_consecutive(msgs)
    assert len(out) == 1
    assert out[0].text == "I think / the stream / is great"


def test_merge_consecutive_breaks_on_author_change() -> None:
    out = merge_consecutive([_m("a", "x"), _m("b", "y"), _m("a", "z")])
    assert [m.author for m in out] == ["a", "b", "a"]
    assert [m.text for m in out] == ["x", "y", "z"]


def test_merge_never_merges_unknown() -> None:
    out = merge_consecutive([_m("unknown", "a"), _m("unknown", "b")])
    assert len(out) == 2  # anonymous authors are not the same person


def test_merge_does_not_mutate_input() -> None:
    a, b = _m("u", "one"), _m("u", "two")
    merge_consecutive([a, b])
    assert a.text == "one" and b.text == "two"


def test_clean_chat_dedup_then_merge() -> None:
    msgs = [
        _m("spam", "buy now"),
        _m("spam", "BUY NOW!!"),  # near-duplicate of the first -> dropped
        _m("spam", "really cheap"),  # new -> kept, merged with the first
        _m("alice", "nice stream"),
    ]
    out = clean_chat(msgs)
    assert len(out) == 2
    assert out[0].author == "spam"
    assert out[0].text == "buy now / really cheap"
    assert out[1].author == "alice"


def test_clean_chat_flags_toggle_each_step() -> None:
    msgs = [_m("u", "hi"), _m("u", "hi"), _m("u", "yo")]
    assert len(clean_chat(msgs, dedupe=False, merge_authors=False)) == 3
    assert len(clean_chat(msgs, dedupe=True, merge_authors=False)) == 2
    assert len(clean_chat(msgs, dedupe=False, merge_authors=True)) == 1


def test_window_config_roundtrips_flags() -> None:
    cfg = WindowConfig.from_dict({"n": 10, "dedupe": False, "merge_authors": False})
    assert cfg.dedupe is False
    assert cfg.merge_authors is False
    d = cfg.to_dict()
    assert d["dedupe"] is False
    assert d["merge_authors"] is False
    assert WindowConfig().dedupe is True
    assert WindowConfig().merge_authors is True


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _t in _tests:
        _t()
        print("ok:", _t.__name__)
    print("ALL TESTS PASSED")
