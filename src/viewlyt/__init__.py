"""viewlyt — scrape a YouTube video's comments (and transcript) with Selenium.

Library quickstart::

    from viewlyt import scrape_video
    r = scrape_video("https://youtu.be/dQw4w9WgXcQ", transcript=True)

For just the dependency-free text helpers (no Selenium import), use
``from viewlyt.htmltext import html_to_text`` directly.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .api import Comment, ScrapeResult, scrape_video
from .driver import build_driver
from .htmltext import (
    flatten_inline,
    format_transcript,
    html_to_text,
    parse_relative_date,
    slugify,
)
from .scraper import (
    BlockedError,
    collect_comments,
    extract_video_id,
    fetch_transcript,
)

try:
    __version__ = version("viewlyt")
except PackageNotFoundError:  # pragma: no cover - running from a source tree w/o install
    __version__ = "0.0.0"

__all__ = [
    # high-level
    "scrape_video",
    "ScrapeResult",
    "Comment",
    # building blocks
    "extract_video_id",
    "build_driver",
    "collect_comments",
    "fetch_transcript",
    "BlockedError",
    # pure text helpers
    "html_to_text",
    "format_transcript",
    "parse_relative_date",
    "flatten_inline",
    "slugify",
    "__version__",
]
