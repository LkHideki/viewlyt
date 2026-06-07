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

from .driver import build_driver
from .htmltext import format_transcript, html_to_text
from .scraper import (
    BlockedError,
    collect_comments,
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
class ScrapeResult:
    """Everything scraped for one video. ``transcript`` is ``[(timestamp, text)]``."""

    video_id: str
    title: str
    comments: list[Comment] = field(default_factory=list)
    transcript: list[tuple[str, str]] = field(default_factory=list)

    @property
    def top_level(self) -> list[Comment]:
        return [c for c in self.comments if c.kind == "comment"]

    @property
    def replies(self) -> list[Comment]:
        return [c for c in self.comments if c.kind == "reply"]

    def transcript_lines(self) -> list[str]:
        """Transcript as ``[ts] text`` lines (see :func:`viewlyt.format_transcript`)."""
        return format_transcript(self.transcript)


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


def scrape_video(
    url: str,
    *,
    comments: bool = True,
    transcript: bool = False,
    limit: int = 150,
    max_viewports: int = 25,
    replies: bool = True,
    max_replies: int = 15,
    headless: bool = True,
    user_data_dir: str | None = None,
) -> ScrapeResult:
    """Scrape one video and return a :class:`ScrapeResult` (writes no files).

    Builds and quits its own Chrome. Raises :class:`viewlyt.BlockedError` if
    YouTube serves a consent/bot wall (retry with ``headless=False`` or a
    logged-in ``user_data_dir``).
    """
    video_id = extract_video_id(url)
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)
    try:
        prime_consent_cookies(driver)
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
        tx = fetch_transcript(driver, progress=False) if transcript else []
    finally:
        try:
            driver.quit()
        except Exception:  # pragma: no cover
            pass
    return ScrapeResult(
        video_id=video_id, title=title, comments=_to_comments(records), transcript=tx
    )
