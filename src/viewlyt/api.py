"""Programmatic API — use viewlyt as a library.

    from viewlyt import scrape_video

    r = scrape_video("https://youtu.be/dQw4w9WgXcQ", transcript=True)
    print(r.title)
    for c in r.comments:
        print(c.author, c.likes, c.date, c.text)
    print("\\n".join(r.transcript_lines()))

``scrape_video`` builds and tears down its own headless Chrome and returns
structured data (nothing is written to disk). For batch use with a reused
browser-instance pool and file output, see :mod:`viewlyt.cli`. The pure text
helpers (``html_to_text``, ``format_transcript``, …) live — dependency-free — in
:mod:`viewlyt.htmltext`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .driver import build_driver
from .htmltext import (
    format_comment_lines,
    format_related,
    format_transcript,
    html_to_text,
    slugify,
)
from .scraper import (
    BlockedError,
    collect_comments,
    collect_related,
    detect_block,
    dismiss_consent_dialog,
    extract_video_id,
    fetch_transcript,
    get_video_title,
    prime_consent_cookies,
    safe_get,
)


@dataclass(slots=True)
class Comment:
    """A single comment or reply, as ready-to-use plain text."""

    kind: str  # "comment" | "reply"
    author: str  # e.g. "@handle" ("" if it couldn't be resolved)
    text: str  # plain text (emoji alt + link text kept; <br> -> newline)
    likes: str  # YouTube's own count, e.g. "842"/"1.2K"; "0" when hidden
    date: str  # relative timestamp as YouTube shows it, e.g. "2 days ago"
    parent_author: str | None = None  # set on replies


@dataclass(slots=True)
class RelatedVideo:
    """One related video from the watch-page sidebar. ``views`` is YouTube's own
    sidebar text (e.g. "1.2B views"); the sidebar exposes NO likes."""

    video_id: str
    title: str
    views: str
    url: str


@dataclass(slots=True)
class ScrapeResult:
    """Everything scraped for one video. ``transcript`` is ``[(timestamp, text)]``."""

    video_id: str
    title: str
    comments: list[Comment] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)
    related: list[RelatedVideo] = field(default_factory=list)
    # Raw scraper records (with HTML), kept so comment_lines()/write() can reuse the
    # exact CLI merge+format pipeline. Private; hidden from repr.
    _records: list[dict] = field(default_factory=list, repr=False)

    @property
    def top_level(self) -> list[Comment]:
        return [c for c in self.comments if c.kind == "comment"]

    @property
    def replies(self) -> list[Comment]:
        return [c for c in self.comments if c.kind == "reply"]

    def comment_lines(self, *, merge: bool = True, today: date | None = None) -> list[str]:
        """Comments as the CLI-formatted text block (merged by default) — identical
        to viewlyt's ``out/<slug>-<id>.txt`` body (see
        :func:`viewlyt.format_comment_lines`)."""
        return format_comment_lines(self._records, today=today, merge=merge)

    def transcript_lines(self) -> list[str]:
        """Transcript as ``[ts] text`` lines (see :func:`viewlyt.format_transcript`)."""
        return format_transcript(self.transcript)

    def related_lines(self) -> list[str]:
        """Related videos as a numbered Markdown list (see :func:`viewlyt.format_related`)."""
        return format_related(
            [{"title": r.title, "views": r.views, "url": r.url} for r in self.related]
        )

    def write(self, out_dir: str, *, merge: bool = True) -> dict[str, Path]:
        """Write the scraped data to ``out_dir`` exactly like the CLI:
        ``<slug>-<id>.txt`` (comments), ``.transcript.txt``, ``.related.txt``.

        Only non-empty sections are written (no 0-byte files). Returns a mapping
        of section name (``"comments"``/``"transcript"``/``"related"``) to the
        written :class:`pathlib.Path`.
        """
        base = Path(out_dir)
        slug = slugify(self.title)
        sections = (
            ("comments", self.comment_lines(merge=merge), ""),
            ("transcript", self.transcript_lines(), ".transcript"),
            ("related", self.related_lines(), ".related"),
        )
        written: dict[str, Path] = {}
        for kind, lines, suffix in sections:
            if not lines:
                continue
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"{slug or 'video'}-{self.video_id}{suffix}.txt"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            written[kind] = path
        return written


def _to_comments(records: list[dict]) -> list[Comment]:
    return [
        Comment(
            kind=r.get("kind", "comment"),
            author=r.get("author") or "",
            text=html_to_text(r.get("html", "")),
            likes=r.get("likes") or "0",
            date=r.get("date_raw") or "",
            parent_author=r.get("parent_author"),
        )
        for r in records
    ]


def _to_related(items: list[dict]) -> list[RelatedVideo]:
    return [
        RelatedVideo(
            video_id=it.get("video_id") or "",
            title=it.get("title") or "",
            views=it.get("views") or "",
            url=it.get("url") or "",
        )
        for it in items
    ]


def _scrape_url(
    driver,
    url: str,
    *,
    comments: bool,
    transcript: bool,
    related: int,
    limit: int,
    max_viewports: int,
    replies: bool,
    max_replies: int,
) -> ScrapeResult:
    """Scrape one video on an already-built, consent-primed ``driver``.

    Shared by :func:`scrape_video`, :class:`Session` and :func:`scrape_videos`.
    Raises :class:`BlockedError` on a consent/bot wall. Does NOT build or quit the
    driver — the caller owns its lifecycle.
    """
    video_id = extract_video_id(url)
    safe_get(driver, f"https://www.youtube.com/watch?v={video_id}")
    dismiss_consent_dialog(driver, timeout=2.0)
    block = detect_block(driver)
    if block:
        raise BlockedError(block)
    title = get_video_title(driver)
    records = (
        collect_comments(
            driver,
            limit=limit,
            max_viewports=max_viewports,
            expand_replies=replies,
            max_replies=max_replies,
            progress=False,
        )
        if comments
        else []
    )
    # Related before transcript: the transcript panel takes over the #secondary
    # column that hosts the related lockups (collect_related never raises).
    rel = collect_related(driver, limit=related, progress=False) if related > 0 else []
    tx = fetch_transcript(driver, progress=False) if transcript else []
    return ScrapeResult(
        video_id=video_id,
        title=title,
        comments=_to_comments(records),
        transcript=tx,
        related=_to_related(rel),
        _records=records,
    )


def scrape_video(
    url: str,
    *,
    comments: bool = True,
    transcript: bool = False,
    related: int = 0,
    limit: int = 150,
    max_viewports: int = 25,
    replies: bool = True,
    max_replies: int = 5,
    headless: bool = True,
    user_data_dir: str | None = None,
) -> ScrapeResult:
    """Scrape one video and return a :class:`ScrapeResult` (writes no files).

    Builds and quits its own Chrome. ``related`` is the number of sidebar related
    videos to collect (0 = none). Raises :class:`viewlyt.BlockedError` if YouTube
    serves a consent/bot wall (retry with ``headless=False`` or a logged-in
    ``user_data_dir``). To scrape several videos on ONE browser, use
    :class:`Session` or :func:`scrape_videos`.
    """
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)
    try:
        prime_consent_cookies(driver)
        return _scrape_url(
            driver,
            url,
            comments=comments,
            transcript=transcript,
            related=related,
            limit=limit,
            max_viewports=max_viewports,
            replies=replies,
            max_replies=max_replies,
        )
    finally:
        try:
            driver.quit()
        except Exception:  # pragma: no cover
            pass
