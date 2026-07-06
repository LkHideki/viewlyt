"""Unified ``vl`` entry point.

A thin, dependency-light dispatcher: the reserved first tokens ``ask``, ``live``,
``split`` and ``watch`` route to the optional subsystems (imported lazily, so
their extra deps are only touched when actually used), and anything else — a
URL/id, a flag, no args — falls through to the default scraper CLI.

    vl '<url>'              -> viewlyt.cli:main   (scrape; the default)
    vl ask out/*.md '<q>'   -> viewlyt.rag:main
    vl live '<live-url>'    -> viewlyt.live.cli:main
    vl split out/*.md       -> viewlyt.split:main
    vl watch                -> viewlyt.watch:main

Kept free of ``viewlyt.cli`` at import time (which pulls in Selenium via
``.driver``/``.scraper``), so ``vl ask``/``vl live``/``vl split``/``vl watch``
don't pay that cost until they actually dispatch a scrape.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SUBCOMMANDS = ("ask", "live", "split", "watch")


def _load_dotenv(path: Path | None = None) -> None:
    """Best-effort load of a ``.env`` (``KEY=VALUE`` per line) into ``os.environ``.

    Stdlib-only (no python-dotenv dep, which is only transitively present under
    the ``live`` extra). A NON-EMPTY value already in the real environment is
    never overridden, so an explicit ``export`` (or a ``--api-key`` on the
    command line) still wins; an env var present but set to an empty string is
    treated as unset, so a shell that exports ``OPENROUTER_API_KEY=`` doesn't
    shadow the ``.env``. Silently does nothing if the file is missing/unreadable
    — the CLI must never crash because of a malformed dotenv.
    """
    env_path = path or Path.cwd() / ".env"
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def _subcommand_main(name: str):
    """Lazily import the entry point for a reserved subcommand (deps only touched here)."""
    if name == "ask":
        from .rag import main as sub_main
    elif name == "split":
        from .split import main as sub_main
    elif name == "watch":
        from .watch import main as sub_main
    else:
        from .live.cli import main as sub_main
    return sub_main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _load_dotenv()

    # `vl help` / `vl help <sub>` -> the matching --help (git-style discoverability).
    if argv and argv[0] == "help":
        rest = argv[1:]
        if rest and rest[0] in _SUBCOMMANDS:
            return _subcommand_main(rest[0])(["--help"])
        argv = ["--help"]
    # A reserved token is a subcommand ONLY as the first argument; anywhere else
    # (e.g. `vl '<url>' live`) it's an ordinary positional handed to the scraper.
    elif argv and argv[0] in _SUBCOMMANDS:
        return _subcommand_main(argv[0])(argv[1:])

    from .cli import main as scrape_main

    return scrape_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
