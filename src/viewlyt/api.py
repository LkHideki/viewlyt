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

import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from queue import Empty, Queue

from .driver import build_driver
from .htmltext import (
    format_comment_lines,
    format_related,
    format_transcript,
    format_unified,
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

log = logging.getLogger("viewlyt")


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

    def _sections(self, *, merge: bool = True) -> list[tuple[str, str, str, list[str]]]:
        """Single source of the product sections, in canonical order:
        ``(kind, header, filename-suffix, lines)``. Drives ``write()`` (separate
        files), ``unified_lines()``/``write(unify=True)``, and is the one place a
        new product type is added so it flows into every output for free."""
        return [
            ("comments", "Comments", "", self.comment_lines(merge=merge)),
            ("transcript", "Transcript", ".transcript", self.transcript_lines()),
            ("related", "Related videos", ".related", self.related_lines()),
        ]

    def unified_lines(self, *, merge: bool = True) -> list[str]:
        """All collected products in ONE document — ``# title`` + ``## section``
        blocks, empty sections skipped (see :func:`viewlyt.format_unified`)."""
        return format_unified(
            self.title,
            [(header, lines) for _kind, header, _suffix, lines in self._sections(merge=merge)],
        )

    def write(self, out_dir: str, *, merge: bool = True, unify: bool = False) -> dict[str, Path]:
        """Write the scraped data to ``out_dir``.

        Default — one file per product, exactly like the CLI: ``<slug>-<id>.txt``
        (comments), ``.transcript.txt``, ``.related.txt`` (only non-empty ones).
        With ``unify=True`` — a single ``<slug>-<id>.unified.txt`` with every
        product instead. Returns a mapping of section name (or ``"unified"``) to
        the written :class:`pathlib.Path`.
        """
        base = Path(out_dir)
        base_name = f"{slugify(self.title) or 'video'}-{self.video_id}"
        written: dict[str, Path] = {}

        if unify:
            lines = self.unified_lines(merge=merge)
            if lines:
                base.mkdir(parents=True, exist_ok=True)
                path = base / f"{base_name}.unified.txt"
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                written["unified"] = path
            return written

        for kind, _header, suffix, lines in self._sections(merge=merge):
            if not lines:
                continue
            base.mkdir(parents=True, exist_ok=True)
            path = base / f"{base_name}{suffix}.txt"
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


class Session:
    """A reusable scraping session over ONE Chrome instance.

    Building Chrome is the expensive part; a ``Session`` builds (and
    consent-primes) it once and scrapes many videos on it, amortising the
    cold-start. Use it as a context manager so the browser is always closed::

        with viewlyt.Session(headless=True) as s:
            a = s.scrape(url1)
            b = s.scrape(url2)          # same browser, no cold-start

    On a consent/bot wall a headless session transparently rebuilds itself headed
    and retries the video once; pass ``fallback=False`` to instead re-raise
    :class:`BlockedError`. The driver is built lazily on the first ``scrape``.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        user_data_dir: str | None = None,
        fallback: bool = True,
    ) -> None:
        self._headless = headless
        self._user_data_dir = user_data_dir
        self._fallback = fallback
        self._driver = None

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def _ensure_driver(self):
        if self._driver is None:
            self._driver = build_driver(headless=self._headless, user_data_dir=self._user_data_dir)
            prime_consent_cookies(self._driver)
        return self._driver

    def scrape(
        self,
        url: str,
        *,
        comments: bool = True,
        transcript: bool = False,
        related: int = 0,
        limit: int = 150,
        max_viewports: int = 25,
        replies: bool = True,
        max_replies: int = 5,
    ) -> ScrapeResult:
        """Scrape one video on this session's (lazily built) browser.

        Raises :class:`BlockedError` only when a block survives the headed retry
        (or when ``fallback=False``).
        """
        kw = dict(
            comments=comments,
            transcript=transcript,
            related=related,
            limit=limit,
            max_viewports=max_viewports,
            replies=replies,
            max_replies=max_replies,
        )
        try:
            return _scrape_url(self._ensure_driver(), url, **kw)
        except BlockedError:
            if self._headless and self._fallback:
                log.warning("blocked on %s — rebuilding this session headed", url)
                self.close()
                self._headless = False
                return _scrape_url(self._ensure_driver(), url, **kw)
            raise

    def close(self) -> None:
        """Quit the browser (idempotent; also called on context-manager exit)."""
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:  # pragma: no cover
                pass
            self._driver = None


def scrape_videos(
    urls: Iterable[str],
    *,
    jobs: int = 4,
    comments: bool = True,
    transcript: bool = False,
    related: int = 0,
    limit: int = 150,
    max_viewports: int = 25,
    replies: bool = True,
    max_replies: int = 5,
    headless: bool = True,
    user_data_dir: str | None = None,
    fallback: bool = True,
) -> list[ScrapeResult | None]:
    """Scrape many videos over a bounded pool of reused browsers.

    Runs ``jobs`` worker threads, each owning ONE reused, consent-primed
    :class:`Session` (Chrome starts once per worker, not once per video). Returns
    a list ALIGNED to the ``urls`` input order: a :class:`ScrapeResult` per
    success, or ``None`` for a video that failed (the error is logged). A poisoned
    session is recycled, so one bad video can't sink the batch.

    WebDriver is single-thread per instance — each worker keeps its own driver and
    they are never shared.
    """
    url_list = list(urls)
    if not url_list:
        return []
    jobs = max(1, min(jobs, len(url_list)))
    kw = dict(
        comments=comments,
        transcript=transcript,
        related=related,
        limit=limit,
        max_viewports=max_viewports,
        replies=replies,
        max_replies=max_replies,
    )

    q: Queue[tuple[int, str]] = Queue()
    for item in enumerate(url_list):
        q.put(item)
    results: list[ScrapeResult | None] = [None] * len(url_list)
    lock = threading.Lock()

    def worker() -> None:
        session = Session(headless=headless, user_data_dir=user_data_dir, fallback=fallback)
        try:
            while True:
                try:
                    idx, url = q.get_nowait()
                except Empty:
                    break
                try:
                    res = session.scrape(url, **kw)
                    with lock:
                        results[idx] = res
                except Exception as exc:  # isolate per-video; recycle the session
                    log.warning("scrape_videos: %r failed: %s", url, exc)
                    session.close()
                finally:
                    q.task_done()
        finally:
            session.close()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
