"""Browser-free tests for the pure helpers. Run: `uv run python tests/test_units.py`."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import tempfile  # noqa: E402
from datetime import date  # noqa: E402

from ytcomments.cli import format_comment_lines, gather_urls, read_urls_from_file  # noqa: E402
from ytcomments.htmltext import (  # noqa: E402
    convert_batch,
    flatten_inline,
    html_to_text,
    parse_relative_date,
    slugify,
)
from ytcomments.scraper import extract_video_id  # noqa: E402

ID = "dQw4w9WgXcQ"


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
    assert (
        html_to_text('great <img alt=":fire:" src="x.png"> video')
        == "great :fire: video"
    )
    # <br> -> newline (preserved inside the block)
    assert html_to_text("line1<br>line2") == "line1\nline2"
    # anchor -> visible text, not href
    assert (
        html_to_text('see <a href="https://www.youtube.com/redirect?q=x">my channel</a>')
        == "see my channel"
    )
    # nested spans (yt-attributed-string style) + trailing whitespace tidy
    assert (
        html_to_text("<span><span>nested </span>text  </span>")
        == "nested text"
    )
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
        {"kind": "comment", "author": "@joao", "html": "Olá <b>mundo</b>", "likes": "842", "date_raw": "2 days ago"},
        {"kind": "reply", "author": "@maria", "parent_author": "@joao", "html": "resposta<br>linha2", "likes": "", "date_raw": "1 day ago"},
        {"kind": "comment", "author": "", "html": "sem autor", "likes": "3", "date_raw": ""},
        {"kind": "comment", "author": "@x", "html": "   ", "likes": "5", "date_raw": "now"},  # empty msg -> skipped
    ]
    lines = format_comment_lines(records, today=today, progress=False)
    assert lines == [
        "@joao [842 likes, 2026-06-04]: Olá mundo",
        "    ↳ (in reply to @joao) @maria [0 likes, 2026-06-05]: resposta linha2",
        "",  # blank line separating blocks
        "unknown [3 likes, unknown]: sem autor",
    ], lines
    print("ok: format_comment_lines")


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


if __name__ == "__main__":
    test_extract_video_id()
    test_html_to_text()
    test_flatten_inline()
    test_slugify()
    test_parse_relative_date()
    test_convert_batch()
    test_format_comment_lines()
    test_url_inputs()
    print("ALL TESTS PASSED")
