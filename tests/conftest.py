"""Shared pytest-only helpers/fakes for the integration, smoke and e2e suites.

tests/test_units.py deliberately keeps its OWN inline fakes so it still runs
standalone via ``python tests/test_units.py`` (which never loads this conftest).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")

# A stable, comment-rich video for the opt-in e2e tests.
VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# Gate the real-browser e2e tests: collected, but skipped unless VIEWLYT_E2E is set.
E2E = pytest.mark.skipif(
    not os.environ.get("VIEWLYT_E2E"),
    reason="real browser + network; set VIEWLYT_E2E=1 to run",
)


class FakeDriver:
    """Minimal stand-in for a Selenium WebDriver — records that quit() was called."""

    def __init__(self) -> None:
        self.quit_calls = 0

    def quit(self) -> None:
        self.quit_calls += 1


def make_comment(author: str, html: str, likes: str = "0", date_raw: str = "just now") -> dict:
    """Build a top-level comment record (the shape collect_comments returns)."""
    return {"kind": "comment", "author": author, "html": html, "likes": likes, "date_raw": date_raw}


def make_reply(
    author: str, parent: str, html: str, likes: str = "0", date_raw: str = "just now"
) -> dict:
    """Build a reply record."""
    return {
        "kind": "reply",
        "author": author,
        "parent_author": parent,
        "html": html,
        "likes": likes,
        "date_raw": date_raw,
    }


def make_scrape_one(table: dict):
    """Return a ``cli.scrape_one`` replacement mapping
    url -> (vid, title, records, transcript, related)."""

    def _scrape_one(driver, url, **kwargs):
        return table[url]

    return _scrape_one


def cli_run(args: list[str]) -> subprocess.CompletedProcess:
    """Run the CLI in a subprocess via ``main(argv)`` and return the CompletedProcess.

    Uses ``sys.executable -c`` with PYTHONPATH=src so it exercises the real argparse
    + exit-code path (``--version`` / ``--help`` SystemExit via argparse) without a
    browser. Mirrors the lazy-import smoke pattern in tests/test_units.py.
    """
    code = "import sys\nfrom viewlyt.cli import main\nsys.exit(main(sys.argv[1:]))\n"
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True, env=env
    )


def cli_run_live(args: list[str]) -> subprocess.CompletedProcess:
    """Like :func:`cli_run`, but for the ``viewlyt-live`` entry point.

    ``viewlyt.live.cli`` imports only stdlib-light modules at parse time (the
    FastAPI server is imported lazily inside main), so --version/--help work
    without the optional 'live' extra installed.
    """
    code = "import sys\nfrom viewlyt.live.cli import main\nsys.exit(main(sys.argv[1:]))\n"
    env = {**os.environ, "PYTHONPATH": SRC}
    return subprocess.run(
        [sys.executable, "-c", code, *args], capture_output=True, text=True, env=env
    )
