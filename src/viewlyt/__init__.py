"""viewlyt — scrape a YouTube video's comments (and transcript) with Selenium.

Library quickstart::

    from viewlyt import scrape_video
    r = scrape_video("https://youtu.be/dQw4w9WgXcQ", transcript=True)

The pure, dependency-free text helpers (``html_to_text``, ``format_transcript``,
``parse_relative_date``, ``flatten_inline``, ``slugify``) and ``__version__`` are
importable WITHOUT pulling in Selenium — ``import viewlyt`` stays lightweight.
The Selenium-backed names (``scrape_video``, ``build_driver``, …) are loaded
lazily on first access (PEP 562), so they cost nothing until you use them.
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

# Pure helpers: stdlib only, safe to import eagerly (no Selenium).
from .htmltext import (
    flatten_inline,
    format_related,
    format_transcript,
    html_to_text,
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
    from .api import Comment, RelatedVideo, ScrapeResult, scrape_video
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
    "format_transcript",
    "format_related",
    "parse_relative_date",
    "flatten_inline",
    "slugify",
    "__version__",
]
