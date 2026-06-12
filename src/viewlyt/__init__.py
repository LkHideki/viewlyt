"""viewlyt — scrape a YouTube video's comments (and transcript) with Selenium.

Library quickstart::

    from viewlyt import scrape_video
    r = scrape_video("https://youtu.be/dQw4w9WgXcQ", transcript=True, related=10)
    print("\\n".join(r.comment_lines()))   # same text as the CLI's .md
    r.write("out/")                         # .md / .transcript.md / .related.md

Many videos on ONE reused browser (amortises Chrome startup)::

    from viewlyt import scrape_videos, Session
    results = scrape_videos(urls, jobs=4)            # list aligned to input order
    with Session() as s:                             # or drive it manually
        a, b = s.scrape(url1), s.scrape(url2)

The pure, dependency-free helpers (``html_to_text``, ``format_comment_lines``,
``format_transcript``, ``format_related``, ``group_consecutive_comments``,
``parse_relative_date``, ``flatten_inline``, ``slugify``) and ``__version__`` are
importable WITHOUT pulling in Selenium — ``import viewlyt`` stays lightweight. The
Selenium-backed names (``scrape_video``, ``scrape_videos``, ``Session``,
``build_driver``, …) are loaded lazily on first access (PEP 562), so they cost
nothing until you use them.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

# Pure helpers: stdlib only, safe to import eagerly (no Selenium).
from .htmltext import (
    flatten_inline,
    format_comment_lines,
    format_related,
    format_transcript,
    format_unified,
    group_consecutive_comments,
    html_to_text,
    join_unified,
    parse_relative_date,
    slugify,
)

try:
    __version__ = version("viewlyt")
except PackageNotFoundError:  # pragma: no cover - running from a source tree w/o install
    __version__ = "0.0.0"

# Selenium-backed names resolved lazily via __getattr__, mapped to their module.
_LAZY = {
    "scrape_video": "api",
    "scrape_videos": "api",
    "Session": "api",
    "ScrapeResult": "api",
    "Comment": "api",
    "RelatedVideo": "api",
    "build_driver": "driver",
    "collect_comments": "scraper",
    "collect_related": "scraper",
    "fetch_transcript": "scraper",
    "extract_video_id": "scraper",
    "BlockedError": "scraper",
}

if TYPE_CHECKING:  # let type checkers and IDEs see the real symbols
    from .api import Comment, RelatedVideo, ScrapeResult, Session, scrape_video, scrape_videos
    from .driver import build_driver
    from .scraper import (
        BlockedError,
        collect_comments,
        collect_related,
        extract_video_id,
        fetch_transcript,
    )


def __getattr__(name: str) -> object:  # PEP 562 module-level lazy attribute access
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value  # cache so __getattr__ runs at most once per name
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    # high-level (lazy)
    "scrape_video",
    "scrape_videos",
    "Session",
    "ScrapeResult",
    "Comment",
    "RelatedVideo",
    # building blocks (lazy)
    "extract_video_id",
    "build_driver",
    "collect_comments",
    "collect_related",
    "fetch_transcript",
    "BlockedError",
    # pure text helpers (eager)
    "html_to_text",
    "format_comment_lines",
    "format_transcript",
    "format_related",
    "format_unified",
    "join_unified",
    "group_consecutive_comments",
    "parse_relative_date",
    "flatten_inline",
    "slugify",
    "__version__",
]
