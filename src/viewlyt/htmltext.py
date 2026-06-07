"""Convert a YouTube comment's ``#content-text`` innerHTML into plain text.

We deliberately do NOT use Selenium's ``element.text``: it silently drops the
meaning of custom emotes/emoji that YouTube renders as ``<img alt=":smile:">``.
Instead we parse the raw innerHTML and rebuild the text, keeping:

* text nodes verbatim (HTML entities are decoded by the parser),
* ``<img>`` -> its ``alt`` (fallback ``aria-label`` / ``shared-tooltip-text``),
* ``<a>`` -> its visible inner text (links, @mentions, #hashtags, timestamps),
* ``<br>`` and block boundaries -> a newline.

This module is pure (no Selenium / no I/O), which is exactly why the CLI can fan
it out across a ``ThreadPoolExecutor``.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from html.parser import HTMLParser

_LINEBREAK_TAGS = {"br"}
_BLOCK_TAGS = {"p", "div"}


class _CommentTextExtractor(HTMLParser):
    """Walk a comment HTML fragment and accumulate readable text."""

    def __init__(self) -> None:
        # convert_charrefs=True decodes &amp; &gt; &#39; ... into the data stream.
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def _newline(self) -> None:
        if self._parts and not self._parts[-1].endswith("\n"):
            self._parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _LINEBREAK_TAGS:
            self._parts.append("\n")
        elif tag == "img":
            a = {k: (v or "") for k, v in attrs}
            alt = a.get("alt") or a.get("aria-label") or a.get("shared-tooltip-text")
            if alt:
                self._parts.append(alt)
        elif tag in _BLOCK_TAGS:
            self._newline()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # Handles self-closing forms like <br/> and <img .../>.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        # Tidy: strip trailing whitespace per line, collapse runs of >2 blank
        # lines, and trim the whole block.
        lines = [ln.rstrip() for ln in text.split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_text(html: str) -> str:
    """Return the plain-text content of a comment HTML fragment.

    Robust to malformed fragments: on a parser error it falls back to a crude
    tag strip so one bad comment never aborts a whole scrape.
    """
    if not html:
        return ""
    parser = _CommentTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - defensive last resort
        import html as _html

        return _html.unescape(re.sub(r"<[^>]+>", "", html)).strip()
    return parser.get_text()


def convert_batch(htmls: list[str]) -> list[str]:
    """Convert a batch of comment HTML fragments to plain text.

    This is the unit of work dispatched to the parallel executor. It lives in
    this dependency-free module so it can be imported inside a subinterpreter
    (``InterpreterPoolExecutor``) without dragging in Selenium. Batching keeps
    cross-interpreter/thread overhead low versus one task per comment.
    """
    return [html_to_text(h) for h in htmls]


def flatten_inline(text: str) -> str:
    """Collapse text to a single line: every whitespace run (incl. newlines)
    becomes one space. Used so each comment occupies exactly one output line."""
    return " ".join(text.split())


_REL_RE = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I)
_UNIT_DAYS = {"second": 0, "minute": 0, "hour": 0, "day": 1, "week": 7, "month": 30, "year": 365}


def parse_relative_date(text: str, today: date) -> str:
    """Convert YouTube's relative comment timestamp to an absolute ``yyyy-mm-dd``.

    YouTube only renders a relative time ("2 days ago", "1 month ago",
    "just now", "3 weeks ago (edited)"), so the absolute date is necessarily an
    APPROXIMATION computed from ``today`` (months≈30d, years≈365d). Returns the
    original text unchanged if it cannot be parsed, or "" for empty input.
    """
    if not text:
        return ""
    t = text.lower().replace("(edited)", "").strip()
    if "just now" in t or "moment" in t:
        return today.isoformat()
    t = re.sub(r"^an?\s+", "1 ", t)  # "a day ago" / "an hour ago" -> "1 ..."
    m = _REL_RE.search(t)
    if not m:
        return text.strip()
    days = int(m.group(1)) * _UNIT_DAYS[m.group(2)]
    return (today - timedelta(days=days)).isoformat()


def slugify(text: str, max_len: int = 80) -> str:
    """Make a filesystem-safe slug from a (possibly accented) video title.

    NFKD-normalises and drops accents (Portuguese titles -> ascii), lowercases,
    turns any non-alphanumeric run into a single '-', trims, and caps length.
    Returns 'video' if nothing usable remains.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "video"
