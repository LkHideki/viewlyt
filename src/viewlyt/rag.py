"""Prepare the collected YouTube products (``out/*.md``) for a knowledge-graph RAG.

This module turns the already-clean ``.md`` files the scraper wrote into
**self-describing documents**: each one gets a context header that names the video
(title, id, url) and summarizes its engagement (comment/reply counts, total likes),
followed by the body. That header is what lets a graph RAG like **LightRAG** tell
which video every comment/topic belongs to — so questions that *compare* videos
("which got more acceptance?", "how do they relate?") have something to bind to —
and it surfaces the numeric "acceptance" up front, since rank-then-read RAG is weak
at aggregating numbers it has to dig out of the text.

The functions here are **pure / stdlib-only** (they restructure text, no network,
no third-party deps), mirroring :mod:`viewlyt.htmltext`. The LightRAG ingest+query
layer (which talks to OpenRouter) lands in the same package but imports ``lightrag``
/ ``openai`` LAZILY — exactly how :mod:`viewlyt.live.llm` defers ``openai`` — so
importing this module never drags those in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

WATCH_URL = "https://www.youtube.com/watch?v="

# An out/ filename's suffix -> product kind; the bare ``.md`` is the comments product.
_KIND_BY_SUFFIX = {"transcript": "transcript", "related": "related", "unified": "unified"}
_KIND_LABEL = {
    "comments": "comments",
    "transcript": "transcript",
    "related": "related videos",
    "unified": "unified (comments + transcript + related)",
}


@dataclass(slots=True)
class RagDocument:
    """One self-describing document ready for LightRAG ``ainsert``.

    ``doc_id`` is stable per source file (it drives LightRAG's content/id dedup so
    re-ingesting the same file is a no-op); ``file_path`` is the citation label fed
    to LightRAG; ``content`` is the header + body text to embed and graph.
    """

    doc_id: str
    file_path: str
    title: str
    video_id: str
    kind: str
    content: str


def parse_out_filename(name: str) -> tuple[str, str, str]:
    """Split a viewlyt output filename into ``(slug, video_id, kind)``.

    Recognizes ``<slug>-<video_id>[.transcript|.related|.unified].md``. The video id
    is the trailing 11-char YouTube token after a hyphen; when it's absent (e.g.
    ``unified-all.md``) the id is ``""`` and the slug is the whole base. ``kind`` is
    ``comments`` for the bare ``.md``. Directory components are ignored.
    """
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.endswith(".md"):
        base = base[:-3]
    if base == "unified-all":  # the --unify-all global file: unified, no per-video id
        return "unified-all", "", "unified"
    kind = "comments"
    for suffix, label in _KIND_BY_SUFFIX.items():
        if base.endswith("." + suffix):
            kind = label
            base = base[: -(len(suffix) + 1)]
            break
    video_id = ""
    slug = base
    m = re.search(r"-([A-Za-z0-9_-]{11})$", base)
    if m:
        video_id = m.group(1)
        slug = base[: m.start()]
    return slug, video_id, kind


# A count token: digits with optional grouping/decimal, then an optional magnitude
# suffix (en ``K/M/B`` or pt-BR ``mil/mi/bi/tri``, e.g. "1.2K", "3,4 mi", "842").
_COUNT_RE = re.compile(r"([\d.,]+)\s*(k|m|b|mil|mi|bi|tri)?\b", re.I)
_COUNT_MULT = {
    "": 1,
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
    "mil": 1_000,
    "mi": 1_000_000,
    "bi": 1_000_000_000,
    "tri": 1_000_000_000_000,
}


def parse_count(token: str) -> int:
    """Parse a YouTube-style count ('1.2K', '3,4 mi', '842', '1,234') into an int.

    With a magnitude suffix the separator is read as a decimal point ("1.2K" ->
    1200); without one it's read as a thousands grouping ("1,234" -> 1234). Returns
    ``0`` on anything unparseable so a stray token never aborts the metrics pass.
    """
    if not token:
        return 0
    m = _COUNT_RE.search(token.strip())
    if not m:
        return 0
    num, suffix = m.group(1), (m.group(2) or "").lower()
    mult = _COUNT_MULT.get(suffix, 1)
    if suffix:
        num = num.replace(",", ".")  # suffix present -> '.'/',' is a decimal point
    else:
        num = num.replace(".", "").replace(",", "")  # bare number -> strip groupings
    try:
        return int(float(num) * mult)
    except ValueError:
        return 0


# Captures the count token inside a "[<count> likes, <date>]" stamp of an output line
# (both top-level ``@user [N likes, ...]`` and ``↳ ... @user [N likes, ...]`` replies).
_LIKES_RE = re.compile(r"\[([^\]]+?) likes, ")


def comment_metrics(text: str) -> dict[str, int]:
    """Tally engagement from a comments/unified ``.md`` body.

    Reads the per-line ``[N likes, date]`` stamps that ``format_comment_lines``
    emits: a line carrying the ↳ reply marker counts as a reply, otherwise a
    top-level comment. Returns ``comments``/``replies`` counts plus the summed and
    single-best like counts (best-effort, since likes are YouTube's own "1.2K"-style
    text). Lines without a stamp (blanks, transcript, related) are ignored.
    """
    comments = replies = total_likes = top_likes = 0
    for line in text.splitlines():
        m = _LIKES_RE.search(line)
        if not m:
            continue
        likes = parse_count(m.group(1))
        total_likes += likes
        top_likes = max(top_likes, likes)
        if "↳" in line:
            replies += 1
        else:
            comments += 1
    return {
        "comments": comments,
        "replies": replies,
        "total_likes": total_likes,
        "top_likes": top_likes,
    }


def _metric_lines(text: str) -> list[str]:
    """Render the engagement tally as Markdown bullets; empty when there's nothing."""
    m = comment_metrics(text)
    if not m["comments"] and not m["replies"]:
        return []
    return [
        f"- comments collected: {m['comments']}",
        f"- replies collected: {m['replies']}",
        f"- total likes (approx): {m['total_likes']}",
        f"- most-liked comment likes: {m['top_likes']}",
    ]


def _derive_title(text: str, slug: str) -> str:
    """The video title: a leading ``# heading`` (unified files keep the real,
    accented title) wins; otherwise de-slugify the filename. Never empty."""
    head = text.lstrip()
    if head.startswith("# "):
        first = head.splitlines()[0][2:].strip()
        if first:
            return first
    return slug.replace("-", " ").strip() or "video"


def build_document(file_path: str, text: str) -> RagDocument:
    """Wrap one product file's text in a video-identifying header -> a RagDocument.

    Pure: ``text`` is the file's content (passed in, not read here). The header names
    the product, video id/url and source file, and — for comments/unified — the
    engagement tally; a body that already opens with a ``# title`` (unified) has that
    duplicate line dropped so the document has a single heading.
    """
    name = Path(file_path).name
    slug, video_id, kind = parse_out_filename(name)
    title = _derive_title(text, slug)

    header = [
        f"# {title}",
        "",
        "## Source metadata",
        "",
        f"- product: {_KIND_LABEL.get(kind, kind)}",
    ]
    if video_id:
        header.append(f"- video_id: {video_id}")
        header.append(f"- url: {WATCH_URL}{video_id}")
    header.append(f"- source_file: {name}")
    if kind in ("comments", "unified"):
        header += _metric_lines(text)

    body = text.strip()
    if body.startswith("# "):  # unified bodies repeat the title -> drop that first line
        body = body.split("\n", 1)[1].lstrip("\n") if "\n" in body else ""
    content = "\n".join(header) + ("\n\n" + body if body else "")
    return RagDocument(
        doc_id=name,
        file_path=str(file_path),
        title=title,
        video_id=video_id,
        kind=kind,
        content=content,
    )


def prepare_documents(paths: Iterable[str | Path]) -> list[RagDocument]:
    """Read each ``out/*.md`` path and build a :class:`RagDocument` from it.

    The only I/O in the pure layer: it reads UTF-8 text and delegates the shaping to
    :func:`build_document`. Unreadable paths are skipped (not fatal). Order follows
    the input; empty/whitespace-only files still yield a header-only document so the
    video is represented in the graph.
    """
    docs: list[RagDocument] = []
    for p in paths:
        path = Path(p)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        docs.append(build_document(str(path), text))
    return docs
