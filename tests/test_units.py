"""Browser-free tests for the pure helpers. Run: `uv run python tests/test_units.py`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tempfile  # noqa: E402
from datetime import date  # noqa: E402

from selenium.common.exceptions import (  # noqa: E402
    NoSuchElementException,
    WebDriverException,
)

from viewlyt.cli import (  # noqa: E402
    build_parser,
    format_comment_lines,
    gather_urls,
    read_urls_from_file,
    resolve_modes,
)
from viewlyt.htmltext import (  # noqa: E402
    convert_batch,
    flatten_inline,
    format_transcript,
    group_consecutive_comments,
    html_to_text,
    parse_relative_date,
    slugify,
)
from viewlyt.scraper import extract_video_id  # noqa: E402

ID = "dQw4w9WgXcQ"
TODAY = date(2026, 6, 6)


class _FakeNode:
    """Duck-typed element node: only the get_attribute() the helpers read."""

    def __init__(self, text: str = "", html: str = "") -> None:
        self._attrs = {"textContent": text, "innerHTML": html}

    def get_attribute(self, name: str) -> str:
        return self._attrs.get(name, "")


class _FakeElement:
    """Maps a CSS selector -> _FakeNode; raises NoSuchElement for anything else."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def find_element(self, _by, css):  # signature matches Selenium (by, value)
        if css in self._mapping:
            return self._mapping[css]
        raise NoSuchElementException(css)


class _StubDriver:
    """Minimal driver exposing execute_script for _comments_disabled tests."""

    def __init__(self, text: str = "", raise_exc: bool = False) -> None:
        self._text = text
        self._raise = raise_exc

    def execute_script(self, _script, *_args):
        if self._raise:
            raise WebDriverException("boom")
        return self._text


def _c(author: str, html: str, likes: str = "0", date_raw: str = "just now") -> dict:
    """Build a top-level comment record."""
    return {"kind": "comment", "author": author, "html": html, "likes": likes, "date_raw": date_raw}


def _r(author: str, parent: str, html: str, likes: str = "0", date_raw: str = "just now") -> dict:
    """Build a reply record."""
    return {
        "kind": "reply",
        "author": author,
        "parent_author": parent,
        "html": html,
        "likes": likes,
        "date_raw": date_raw,
    }


def test_extract_video_id() -> None:
    cases = {
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ": ID,
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=abc": ID,
        "https://youtu.be/dQw4w9WgXcQ": ID,
        "https://youtu.be/dQw4w9WgXcQ?t=10": ID,
        "https://www.youtube.com/shorts/dQw4w9WgXcQ": ID,
        "https://www.youtube.com/embed/dQw4w9WgXcQ": ID,
        "https://www.youtube.com/live/dQw4w9WgXcQ": ID,
        "http://m.youtube.com/watch?v=dQw4w9WgXcQ": ID,
        "youtube.com/watch?v=dQw4w9WgXcQ": ID,
        "dQw4w9WgXcQ": ID,
    }
    for url, expected in cases.items():
        got = extract_video_id(url)
        assert got == expected, f"{url!r} -> {got!r} != {expected!r}"

    for bad in ("", "https://example.com/", "not a url"):
        try:
            extract_video_id(bad)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected ValueError for {bad!r}")
    print("ok: extract_video_id")


def test_html_to_text() -> None:
    # plain text + entity decoding
    assert html_to_text("hello &amp; bye &lt;3") == "hello & bye <3"
    # emote image -> alt text
    assert html_to_text('great <img alt=":fire:" src="x.png"> video') == "great :fire: video"
    # <br> -> newline (preserved inside the block)
    assert html_to_text("line1<br>line2") == "line1\nline2"
    # anchor -> visible text, not href
    assert (
        html_to_text('see <a href="https://www.youtube.com/redirect?q=x">my channel</a>')
        == "see my channel"
    )
    # nested spans (yt-attributed-string style) + trailing whitespace tidy
    assert html_to_text("<span><span>nested </span>text  </span>") == "nested text"
    # self-closing img and br
    assert html_to_text("a<br/>b<img alt='X'/>") == "a\nbX"
    # empty / whitespace
    assert html_to_text("") == ""
    assert html_to_text("   ") == ""
    print("ok: html_to_text")


def test_flatten_inline() -> None:
    assert flatten_inline("line1\nline2") == "line1 line2"
    assert flatten_inline("a\n\n\nb   c\t d") == "a b c d"
    assert flatten_inline("  trimmed  ") == "trimmed"
    assert flatten_inline("") == ""
    # full pipeline: html -> text -> single line
    assert flatten_inline(html_to_text("hi<br>there<br><br>friend")) == "hi there friend"
    print("ok: flatten_inline")


def test_slugify() -> None:
    assert slugify("Hello, World!") == "hello-world"
    # accents stripped (Portuguese titles)
    assert slugify("Atenção: OVNIs à noite") == "atencao-ovnis-a-noite"
    assert slugify("  multiple   spaces  ") == "multiple-spaces"
    assert slugify("--edge--") == "edge"
    assert slugify("") == "video"
    assert slugify("🔥🔥🔥") == "video"
    # length cap, no trailing hyphen
    long = slugify("a " * 100, max_len=10)
    assert len(long) <= 10 and not long.endswith("-")
    print("ok: slugify")


def test_parse_relative_date() -> None:
    today = date(2026, 6, 6)
    assert parse_relative_date("2 days ago", today) == "2026-06-04"
    assert parse_relative_date("1 week ago", today) == "2026-05-30"
    assert parse_relative_date("3 weeks ago (edited)", today) == "2026-05-16"
    assert parse_relative_date("1 month ago", today) == "2026-05-07"  # ~30d
    assert parse_relative_date("1 year ago", today) == "2025-06-06"  # ~365d
    assert parse_relative_date("5 hours ago", today) == "2026-06-06"  # same day
    assert parse_relative_date("just now", today) == "2026-06-06"
    assert parse_relative_date("a day ago", today) == "2026-06-05"
    assert parse_relative_date("an hour ago", today) == "2026-06-06"
    assert parse_relative_date("", today) == ""
    # unparseable -> returned as-is so no data is lost
    assert parse_relative_date("ontem", today) == "ontem"
    print("ok: parse_relative_date")


def test_convert_batch() -> None:
    assert convert_batch(["a<br>b", "<b>x</b> &amp; y"]) == ["a\nb", "x & y"]
    assert convert_batch([]) == []
    print("ok: convert_batch")


def test_format_comment_lines() -> None:
    today = date(2026, 6, 6)
    records = [
        {
            "kind": "comment",
            "author": "@joao",
            "html": "Olá <b>mundo</b>",
            "likes": "842",
            "date_raw": "2 days ago",
        },
        {
            "kind": "reply",
            "author": "@maria",
            "parent_author": "@joao",
            "html": "resposta<br>linha2",
            "likes": "",
            "date_raw": "1 day ago",
        },
        {"kind": "comment", "author": "", "html": "sem autor", "likes": "3", "date_raw": ""},
        {
            "kind": "comment",
            "author": "@x",
            "html": "   ",
            "likes": "5",
            "date_raw": "now",
        },  # empty msg -> skipped
    ]
    lines = format_comment_lines(records, today=today, progress=False)
    assert lines == [
        "@joao [842 likes, 2026-06-04]: Olá mundo",
        "    ↳ (in reply to @joao) @maria [0 likes, 2026-06-05]: resposta linha2",
        "",  # blank line separating blocks
        "unknown [3 likes, unknown]: sem autor",
    ], lines
    print("ok: format_comment_lines")


def test_merge_two_consecutive_same_author() -> None:
    # Two consecutive comments by the same author merge into one block; the FIRST
    # comment's likes+date are kept and the texts are concatenated (br -> space).
    recs = [
        _c("@joao", "primeira", likes="10", date_raw="2 days ago"),
        _c("@joao", "segunda", likes="99", date_raw="just now"),
    ]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "@joao [10 likes, 2026-06-04]: primeira segunda"
    ]
    print("ok: merge_two_consecutive_same_author")


def test_merge_same_author_not_consecutive() -> None:
    # Same author, but interrupted by another author -> NOT merged.
    recs = [
        _c("@joao", "um", likes="1"),
        _c("@maria", "dois", likes="2"),
        _c("@joao", "tres", likes="3"),
    ]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "@joao [1 likes, 2026-06-06]: um",
        "",
        "@maria [2 likes, 2026-06-06]: dois",
        "",
        "@joao [3 likes, 2026-06-06]: tres",
    ]
    print("ok: merge_same_author_not_consecutive")


def test_merge_different_authors() -> None:
    recs = [_c("@a", "x", likes="1"), _c("@b", "y", likes="2")]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "@a [1 likes, 2026-06-06]: x",
        "",
        "@b [2 likes, 2026-06-06]: y",
    ]
    print("ok: merge_different_authors")


def test_merge_anonymous_authors_not_merged() -> None:
    # '' and 'unknown' both render as "unknown" but must NEVER merge/dedup together
    # (two anonymous comments are not "the same author").
    recs = [
        _c("", "primeiro anon", likes="1"),
        _c("", "segundo anon", likes="2"),
        _c("unknown", "terceiro", likes="3"),
        _c("unknown", "quarto", likes="4"),
    ]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "unknown [1 likes, 2026-06-06]: primeiro anon",
        "",
        "unknown [2 likes, 2026-06-06]: segundo anon",
        "",
        "unknown [3 likes, 2026-06-06]: terceiro",
        "",
        "unknown [4 likes, 2026-06-06]: quarto",
    ]
    print("ok: merge_anonymous_authors_not_merged")


def test_merge_exact_duplicate_dropped() -> None:
    # Exact-duplicate top-level comment (markup/whitespace/case-insensitive on the
    # rendered text) is dropped even when not adjacent; the between-comment survives.
    recs = [
        _c("@a", "<b>same</b> text", likes="5"),
        _c("@b", "between", likes="1"),
        _c("@a", "same text", likes="999", date_raw="2 days ago"),
    ]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "@a [5 likes, 2026-06-06]: same text",
        "",
        "@b [1 likes, 2026-06-06]: between",
    ]
    print("ok: merge_exact_duplicate_dropped")


def test_merge_replies_concatenated_across_merge() -> None:
    # Merging two same-author comments keeps ALL replies, in order, under the block.
    recs = [
        _c("@joao", "parte1", likes="7"),
        _r("@maria", "@joao", "resposta A"),
        _c("@joao", "parte2", likes="0"),
        _r("@pedro", "@joao", "resposta B"),
    ]
    assert format_comment_lines(recs, today=TODAY, progress=False) == [
        "@joao [7 likes, 2026-06-06]: parte1 parte2",
        "    ↳ (in reply to @joao) @maria [0 likes, 2026-06-06]: resposta A",
        "    ↳ (in reply to @joao) @pedro [0 likes, 2026-06-06]: resposta B",
    ]
    print("ok: merge_replies_concatenated_across_merge")


def test_merge_disabled_old_behavior() -> None:
    # merge_comments=False reproduces the old verbatim behavior (no merge/dedup).
    recs = [_c("@joao", "um", likes="1"), _c("@joao", "dois", likes="2")]
    assert format_comment_lines(recs, today=TODAY, progress=False, merge_comments=False) == [
        "@joao [1 likes, 2026-06-06]: um",
        "",
        "@joao [2 likes, 2026-06-06]: dois",
    ]
    print("ok: merge_disabled_old_behavior")


def test_group_consecutive_comments_shape() -> None:
    # Direct test of the pure transform: 3x @a collapse to one comment carrying
    # both replies; @b stays separate; input dicts are not mutated.
    recs = [
        _c("@a", "a1"),
        _r("@x", "@a", "r1"),
        _c("@a", "a2"),
        _r("@y", "@a", "r2"),
        _c("@a", "a3"),
        _c("@b", "b1"),
    ]
    out = group_consecutive_comments(recs)
    assert [r["kind"] for r in out] == ["comment", "reply", "reply", "comment"]
    assert out[0]["html"] == "a1<br>a2<br>a3"
    assert out[3]["author"] == "@b"
    assert recs[0]["html"] == "a1"  # input not mutated
    print("ok: group_consecutive_comments_shape")


def test_resolve_modes() -> None:
    # (comments, transcript, transcript_only) -> (with_comments, with_transcript)
    assert resolve_modes(False, False, False) == (True, False)  # no flags -> comments only
    assert resolve_modes(True, False, False) == (True, False)  # -c -> comments only
    assert resolve_modes(False, True, False) == (False, True)  # -t -> transcript only
    assert resolve_modes(True, True, False) == (True, True)  # -c -t -> both
    assert resolve_modes(False, False, True) == (False, True)  # --transcript-only
    assert resolve_modes(True, False, True) == (False, True)  # --transcript-only wins over -c
    print("ok: resolve_modes")


def test_flag_plumbing() -> None:
    p = build_parser()
    # merge is ON by default; both spellings of the disable flag turn it off.
    assert p.parse_args([]).merge_comments is True
    assert p.parse_args(["--no-merge-comments"]).merge_comments is False
    assert p.parse_args(["--prevent-comment-group"]).merge_comments is False
    # selectors land on the expected dests
    a = p.parse_args(["-c", "-t"])
    assert a.comments is True and a.transcript is True
    assert p.parse_args(["--transcript-only"]).transcript_only is True
    # new defaults are wired into the parser
    d = p.parse_args([])
    assert d.limit == 150 and d.max_replies == 15
    print("ok: flag_plumbing")


def test_first_text_and_inner_html() -> None:
    from viewlyt.scraper import _first_inner_html, _first_text

    el = _FakeElement(
        {"b": _FakeNode(text="hit-b"), "c": _FakeNode(text="hit-c", html="<i>hc</i>")}
    )
    assert _first_text(el, ("a", "b", "c")) == "hit-b"  # first matching selector wins
    assert _first_text(el, ("a", "z")) == ""  # none match -> ""
    assert _first_inner_html(el, ("a", "c")) == "<i>hc</i>"
    print("ok: first_text_and_inner_html")


def test_first_inner_html_skips_blank() -> None:
    from viewlyt.scraper import _first_inner_html

    # a whitespace-only innerHTML must not shadow a later populated alternate
    el = _FakeElement({"blank": _FakeNode(html="   "), "real": _FakeNode(html="<b>x</b>")})
    assert _first_inner_html(el, ("blank", "real")) == "<b>x</b>"
    print("ok: first_inner_html_skips_blank")


def test_likes_fallback() -> None:
    from viewlyt.scraper import LIKES_SELECTORS, _likes

    assert _likes(_FakeElement({})) == "0"  # nothing matches -> "0"
    assert LIKES_SELECTORS[1] == "#vote-count-left"  # documents the fallback order
    assert _likes(_FakeElement({"#vote-count-left": _FakeNode(text="42")})) == "42"
    print("ok: likes_fallback")


def test_top_el_fallback() -> None:
    from viewlyt.scraper import _top_el

    thread = _FakeElement({})  # no TOP_COMMENT_SELECTORS match
    assert _top_el(thread) is thread  # falls back to the thread element itself
    print("ok: top_el_fallback")


def test_comments_disabled() -> None:
    from viewlyt.scraper import _comments_disabled

    assert _comments_disabled(_StubDriver("Comments are turned off")) is True
    assert _comments_disabled(_StubDriver("Os comentários estão desativados")) is True
    assert _comments_disabled(_StubDriver("just some normal comments")) is False
    assert _comments_disabled(_StubDriver(raise_exc=True)) is False  # never raises
    print("ok: comments_disabled")


def test_url_inputs() -> None:
    with tempfile.TemporaryDirectory() as d:
        txt = Path(d) / "urls.txt"
        txt.write_text(
            "# comentário, ignorar\n"
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "\n"
            "https://youtu.be/TgMJUAo-tWA\n",
            encoding="utf-8",
        )
        assert read_urls_from_file(str(txt)) == [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/TgMJUAo-tWA",
        ]

        csvf = Path(d) / "urls.csv"
        csvf.write_text(
            "title,url\n"
            "Rick,https://youtu.be/dQw4w9WgXcQ\n"
            "Shorts,https://www.youtube.com/shorts/TgMJUAo-tWA\n",
            encoding="utf-8",
        )
        cells = read_urls_from_file(str(csvf))
        assert "https://youtu.be/dQw4w9WgXcQ" in cells and "title" in cells

        # gather: positional URLs + a file, deduped by video id, order preserved.
        targets = gather_urls(
            ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "not-a-url", str(txt)],
            [str(csvf)],
        )
        ids = [vid for vid, _ in targets]
        assert ids == ["dQw4w9WgXcQ", "TgMJUAo-tWA"], ids  # deduped, in first-seen order
    print("ok: url_inputs")


def test_resolve_chrome_binary() -> None:
    import os

    from viewlyt.driver import CHROME_BINARY_ENV, _resolve_chrome_binary

    # An explicit env override wins and is returned verbatim.
    saved = os.environ.get(CHROME_BINARY_ENV)
    try:
        os.environ[CHROME_BINARY_ENV] = "/custom/path/to/chrome"
        assert _resolve_chrome_binary() == "/custom/path/to/chrome"
    finally:
        if saved is None:
            os.environ.pop(CHROME_BINARY_ENV, None)
        else:
            os.environ[CHROME_BINARY_ENV] = saved

    # Without an override it returns a path string or None (never raises).
    os.environ.pop(CHROME_BINARY_ENV, None)
    got = _resolve_chrome_binary()
    assert got is None or isinstance(got, str)
    print("ok: resolve_chrome_binary")


def test_lazy_import_no_selenium() -> None:
    """`import viewlyt` and the pure helpers must NOT drag in Selenium."""
    import os
    import subprocess

    code = (
        "import sys, viewlyt\n"
        "assert 'selenium' not in sys.modules, 'selenium imported by `import viewlyt`'\n"
        "viewlyt.slugify('x'); viewlyt.html_to_text('<b>x</b>'); _ = viewlyt.__version__\n"
        "assert 'selenium' not in sys.modules, 'selenium imported by a pure helper'\n"
        "from viewlyt import scrape_video\n"  # this one is allowed to pull selenium
        "assert 'selenium' in sys.modules, 'scrape_video should load selenium lazily'\n"
        "print('lazy-import OK')\n"
    )
    src = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"lazy import test failed:\n{r.stdout}\n{r.stderr}"
    print("ok: lazy_import_no_selenium")


def test_format_transcript() -> None:
    segs = [
        ("0:05", "hi there"),
        ("1:02", "  multi\nline   text "),  # whitespace collapsed
        ("1:02", "  multi\nline   text "),  # exact duplicate MUST be kept (no dedup)
        ("1:10:33", "[Music]"),  # long-video h:mm:ss verbatim; marker kept
        ("  3:07  ", "padded ts"),  # timestamp padding trimmed (verbatim otherwise)
        ("2:00", "   "),  # empty after collapse -> dropped
        ("", "sem timestamp"),  # missing ts -> just text
    ]
    out = format_transcript(segs)
    assert out == [
        "[0:05] hi there",
        "[1:02] multi line text",
        "[1:02] multi line text",
        "[1:10:33] [Music]",
        "[3:07] padded ts",
        "sem timestamp",
    ], out
    assert format_transcript([]) == []
    print("ok: format_transcript")


if __name__ == "__main__":
    test_extract_video_id()
    test_html_to_text()
    test_flatten_inline()
    test_slugify()
    test_parse_relative_date()
    test_convert_batch()
    test_format_comment_lines()
    test_merge_two_consecutive_same_author()
    test_merge_same_author_not_consecutive()
    test_merge_different_authors()
    test_merge_anonymous_authors_not_merged()
    test_merge_exact_duplicate_dropped()
    test_merge_replies_concatenated_across_merge()
    test_merge_disabled_old_behavior()
    test_group_consecutive_comments_shape()
    test_resolve_modes()
    test_flag_plumbing()
    test_first_text_and_inner_html()
    test_first_inner_html_skips_blank()
    test_likes_fallback()
    test_top_el_fallback()
    test_comments_disabled()
    test_format_transcript()
    test_url_inputs()
    test_resolve_chrome_binary()
    test_lazy_import_no_selenium()
    print("ALL TESTS PASSED")
