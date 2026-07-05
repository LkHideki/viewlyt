"""Unified ``vl`` entry point.

A thin, dependency-light dispatcher: the reserved first tokens ``ask``, ``live``
and ``split`` route to the optional subsystems (imported lazily, so their extra
deps are only touched when actually used), and anything else — a URL/id, a flag,
no args — falls through to the default scraper CLI.

    vl '<url>'              -> viewlyt.cli:main   (scrape; the default)
    vl ask out/*.md '<q>'   -> viewlyt.rag:main
    vl live '<live-url>'    -> viewlyt.live.cli:main
    vl split out/*.md       -> viewlyt.split:main

Kept free of ``viewlyt.cli`` at import time (which pulls in Selenium via
``.driver``/``.scraper``), so ``vl ask``/``vl live``/``vl split`` don't pay that cost.
"""

from __future__ import annotations

import sys

_SUBCOMMANDS = ("ask", "live", "split")


def _subcommand_main(name: str):
    """Lazily import the entry point for a reserved subcommand (deps only touched here)."""
    if name == "ask":
        from .rag import main as sub_main
    elif name == "split":
        from .split import main as sub_main
    else:
        from .live.cli import main as sub_main
    return sub_main


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

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
