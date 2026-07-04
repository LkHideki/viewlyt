"""Unified ``vl`` entry point.

A thin, dependency-light dispatcher: the two reserved first tokens ``ask`` and
``live`` route to the optional subsystems (imported lazily, so their extra deps
are only touched when actually used), and anything else — a URL/id, a flag, no
args — falls through to the default scraper CLI.

    vl '<url>'              -> viewlyt.cli:main   (scrape; the default)
    vl ask out/*.md '<q>'   -> viewlyt.rag:main
    vl live '<live-url>'    -> viewlyt.live.cli:main

Kept free of ``viewlyt.cli`` at import time (which pulls in Selenium via
``.driver``/``.scraper``), so ``vl ask``/``vl live`` don't pay that cost.
"""

from __future__ import annotations

import sys

_SUBCOMMANDS = ("ask", "live")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in _SUBCOMMANDS:
        sub, rest = argv[0], argv[1:]
        if sub == "ask":
            from .rag import main as sub_main
        else:
            from .live.cli import main as sub_main
        return sub_main(rest)
    from .cli import main as scrape_main

    return scrape_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
