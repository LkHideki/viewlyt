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
from collections.abc import Callable
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


# Authors we treat as "not a real, identifiable person": never merge or dedup
# two of these together (two anonymous comments are not the same author).
_ANON_AUTHORS = {"", "unknown"}


def _comment_key(author: str, html: str) -> tuple[str, str]:
    """Normalized identity of a top-level comment for merge/dedup comparison.

    Compares on the *rendered* plain text, not raw HTML: ``<b>x</b>`` and ``x``
    render identically and should count as the same message, while markup noise
    must not split an otherwise-identical comment. The text is run through the
    same html_to_text + flatten_inline pipeline used for output, then lowercased
    so case-only differences collapse. The author is matched verbatim (handles
    are case-sensitive identifiers; we only special-case the anonymous set).
    """
    text = flatten_inline(html_to_text(html or "")).lower()
    return (author or "", text)


def group_consecutive_comments(records: list[dict]) -> list[dict]:
    """Merge consecutive same-author top-level comments and drop exact dups.

    Pure / stdlib-only (lives here so it stays Selenium-free and unit-testable).

    Input is the flat, ordered record list the scraper produces: each top-level
    comment ``{kind:'comment', author, html, likes, date_raw}`` is immediately
    followed by its replies ``{kind:'reply', author, parent_author, html, ...}``.
    The function regroups each comment with its trailing replies into a block and:

    * **Merges** a block into the previous one when BOTH top-level authors are
      equal and are real (non-empty, not ``"unknown"``). The merged comment keeps
      the FIRST comment's ``likes`` and ``date_raw``; the HTML fragments are joined
      with ``<br>`` (so html_to_text yields a newline that flatten_inline turns
      into a single space); and ALL replies from every merged comment are kept,
      in order.
    * **Drops** a block whose top-level comment is an EXACT duplicate (same author
      AND same rendered text) of one already kept — comparison is top-level only,
      so replies never affect the duplicate decision; a dropped comment's replies
      go with it.

    Returns a NEW flat record list in the same shape, so ``format_comment_lines``
    works unchanged when fed the result. Records are not mutated in place.
    """
    if not records:
        return []

    # 1) Split the flat list into blocks: a 'comment' starts a block; every
    #    following 'reply' (until the next 'comment') belongs to it. Any leading
    #    replies with no parent comment are passed through untouched.
    blocks: list[dict] = []  # {"comment": dict, "replies": list[dict]}
    leading: list[dict] = []
    for r in records:
        if r.get("kind") == "comment":
            blocks.append({"comment": dict(r), "replies": []})
        elif blocks:
            blocks[-1]["replies"].append(dict(r))
        else:
            leading.append(dict(r))  # orphan reply before any comment

    # 2) Walk blocks, merging consecutive same-(real)-author and dropping dups.
    kept: list[dict] = []  # same block shape as above
    seen_keys: set[tuple[str, str]] = set()
    for blk in blocks:
        c = blk["comment"]
        author = c.get("author") or ""
        is_real = author.lower() not in _ANON_AUTHORS
        key = _comment_key(author, c.get("html", ""))

        if is_real and key in seen_keys:
            continue  # exact duplicate top-level comment -> drop block (+replies)

        prev = kept[-1]["comment"] if kept else None
        if (
            is_real
            and prev is not None
            and (prev.get("author") or "") == author
            and (prev.get("author") or "").lower() not in _ANON_AUTHORS
        ):
            # Consecutive same real author: merge into the previous block.
            prev["html"] = f"{prev.get('html', '')}<br>{c.get('html', '')}"
            kept[-1]["replies"].extend(blk["replies"])
        else:
            kept.append({"comment": c, "replies": list(blk["replies"])})

        if is_real:
            seen_keys.add(key)

    # 3) Flatten back to the input shape: comment then its replies, in order.
    out: list[dict] = list(leading)
    for blk in kept:
        out.append(blk["comment"])
        out.extend(blk["replies"])
    return out


def format_transcript(segments: list[tuple[str, str]]) -> list[str]:
    """Format ``(timestamp, text)`` transcript segments as ``[ts] text`` lines.

    Pure/testable. Rules: collapse each segment's whitespace to single spaces;
    drop a segment only when its text is empty AFTER collapsing (so ``[Music]`` /
    ``♪`` markers survive); emit the timestamp VERBATIM (``m:ss`` or ``h:mm:ss``
    for long videos — never reformatted); and NEVER deduplicate (choruses and
    repeated phrases are legitimate). Segments with no timestamp emit just text.
    """
    lines: list[str] = []
    for ts, text in segments:
        text = " ".join((text or "").split())
        if not text:
            continue
        ts = (ts or "").strip()  # verbatim time field, only trim padding
        lines.append(f"[{ts}] {text}" if ts else text)
    return lines


# The [m:ss] / [mm:ss] prefix format_transcript emits (a trailing space included),
# stripped by `strip_timestamps` for the CLI's --no-ts. Note: only minute:second
# stamps match — h:mm:ss (long videos) is left intact by design (this exact regex).
_TIMESTAMP_RE = re.compile(r"\[\d?\d:\d\d\] ")


def strip_timestamps(lines: list[str]) -> list[str]:
    """Drop ``[m:ss]``/``[mm:ss]`` timestamp prefixes from transcript lines.

    Pure/testable; powers ``--no-ts``. Removes every ``\\[\\d?\\d:\\d\\d\\] ``
    match and strips each line's surrounding whitespace.
    """
    return [_TIMESTAMP_RE.sub("", ln).strip() for ln in lines]


def pair_lines(lines: list[str], per_line: int = 2, sep: str = " ") -> list[str]:
    """Join every ``per_line`` consecutive lines into one (token saver: halves
    the ``\\n`` count at the default 2).

    Pure/testable; powers the CLI's default transcript output. Empty lines are
    dropped before pairing (they carry no content and would double ``sep``);
    ``per_line <= 1`` returns the (cleaned) lines unchanged. The last group may
    be shorter when the count isn't a multiple of ``per_line``.
    """
    cleaned = [ln for ln in lines if ln]
    if per_line <= 1:
        return cleaned
    return [sep.join(cleaned[i : i + per_line]) for i in range(0, len(cleaned), per_line)]


def format_related(items: list[dict]) -> list[str]:
    """Format related-video records as a 1-based numbered Markdown list.

    Each item is ``{"title": str, "views": str, "url": str}`` (the shape
    :func:`viewlyt.scraper.collect_related` returns). Produces lines like::

        1. [1.2B views. Video Title](https://www.youtube.com/watch?v=ID)

    ``views`` is YouTube's own sidebar text kept VERBATIM — it already includes
    the word "views"/"visualizações", so nothing is appended (avoids a double
    "views views"). When ``views`` is empty the count prefix is dropped:
    ``1. [Title](url)``. The title is flattened to a single line; items with an
    empty title are skipped (and don't consume a number, so numbering stays
    contiguous).

    KNOWN LIMITATION: a title containing ``]`` (e.g. "[Official Video]") breaks
    the Markdown link syntactically — kept as-is by design (the text is still
    readable), like the approximate dates elsewhere. Treat the file as plain text.
    """
    lines: list[str] = []
    n = 0
    for it in items:
        title = flatten_inline(it.get("title") or "")
        if not title:
            continue
        n += 1
        url = (it.get("url") or "").strip()
        views = " ".join((it.get("views") or "").split())
        label = f"{views}. {title}" if views else title
        lines.append(f"{n}. [{label}]({url})")
    return lines


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


REPLY_INDENT = "    ↳ "  # 4 spaces + ↳; the reply-line prefix in the comment output


def format_comment_lines(
    records: list[dict],
    *,
    today: date | None = None,
    merge: bool = True,
    convert: Callable[[list[str]], list[str]] | None = None,
) -> list[str]:
    """Render comment records as text lines grouped into blocks (a top-level
    comment followed by its replies), with a blank line between blocks. Replies
    are indented and name their parent.

    ``records`` is the flat dict stream the scraper produces (``kind``/``author``/
    ``html``/``likes``/``date_raw``, plus ``parent_author`` on replies). When
    ``merge`` is true (the default) consecutive same-real-author top-level
    comments are merged and exact duplicates dropped via
    :func:`group_consecutive_comments`.

    ``convert`` maps the list of comment HTML fragments to plain text, in order;
    it defaults to :func:`convert_batch` (a simple in-order loop). The CLI injects
    a batched ``ThreadPoolExecutor`` variant for its progress bar — the output is
    byte-for-byte identical (guaranteed by a test).
    """
    if not records:
        return []
    today = today or date.today()
    convert = convert or convert_batch

    if merge:
        records = group_consecutive_comments(records)

    texts = convert([r.get("html", "") for r in records])

    blocks: list[list[str]] = []
    current: list[str] = []
    seen: set[str] = set()
    for r, text in zip(records, texts, strict=True):
        message = flatten_inline(text)
        author = r.get("author") or "unknown"
        likes = r.get("likes") or "0"
        when = parse_relative_date(r.get("date_raw", ""), today) or "unknown"

        if r.get("kind") == "reply":
            if not message:
                continue
            parent = r.get("parent_author") or "unknown"
            line = (
                f"{REPLY_INDENT}(in reply to {parent}) {author} [{likes} likes, {when}]: {message}"
            )
            if line in seen:  # belt-and-suspenders against any repeated element
                continue
            seen.add(line)
            current.append(line)
        else:
            # comment boundary: flush the previous block, then start a fresh one
            if current:
                blocks.append(current)
            current = []
            if not message:
                continue
            line = f"{author} [{likes} likes, {when}]: {message}"
            seen.add(line)
            current.append(line)
    if current:
        blocks.append(current)

    out: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            out.append("")  # blank line separating blocks
        out.extend(block)
    return out


def format_unified(title: str, sections: list[tuple[str, list[str]]]) -> list[str]:
    """Combine a video's product sections into one document under a title and
    per-section Markdown headers.

    PRODUCT-AGNOSTIC: pass ``[(header, lines), ...]`` in the desired order; an
    empty section is skipped entirely (no header), consistent with the "no 0-byte
    files" rule. A future product type flows in by just adding a ``(header, lines)``
    pair — no other change. Layout::

        # <title>

        ## Comments
        <comment lines>

        ## Transcript
        <transcript lines>
    """
    out: list[str] = []
    if title:
        out.append(f"# {title}")
    for header, lines in sections:
        if not lines:
            continue
        if out:
            out.append("")  # blank line before each section
        out.append(f"## {header}")
        out.append("")
        out.extend(lines)
    return out


def join_unified(blocks: list[list[str]]) -> list[str]:
    """Concatenate several per-video unified blocks (each from :func:`format_unified`)
    into one document, blocks separated by a blank line; empty blocks are skipped.

    Powers ``--unify-all`` (and the library equivalent over a list of videos)."""
    out: list[str] = []
    for block in blocks:
        if not block:
            continue
        if out:
            out.append("")
        out.extend(block)
    return out
