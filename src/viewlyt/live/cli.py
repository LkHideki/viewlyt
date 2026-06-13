"""Command-line entry point for viewlyt-live (real-time YouTube live-chat analysis).

Launches a FastAPI dashboard on --host:--port, optionally auto-opens it in a browser,
and displays URLs for the YouTube popout chat and the browser snippet. Imports the
server module lazily so FastAPI/uvicorn only load at run time.
"""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version

from .llm import LLMConfig
from .window import WindowConfig


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for viewlyt-live."""
    parser = argparse.ArgumentParser(
        prog="viewlyt-live",
        description="Real-time YouTube live-chat analysis with LLMs.",
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="YouTube live URL or video id (optional)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Dashboard host (default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Dashboard port (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:1234/v1",
        help="LLM base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--api-key",
        default="lm-studio",
        help="LLM API key (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default="local-model",
        help="LLM model name (default: %(default)s)",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=80,
        help="Window size (number of messages, default: %(default)s)",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=20,
        help="Window overlap (default: %(default)s)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=15.0,
        help="Time gap for snapshot (seconds, default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["count", "time", "hybrid"],
        default="count",
        help="Snapshot policy (default: %(default)s)",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open the dashboard in a browser",
    )

    # Version flag
    try:
        pkg_version = version("viewlyt")
    except PackageNotFoundError:
        pkg_version = "0.0.0"
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {pkg_version}",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, configure LLM/window, and run the server.

    Args:
        argv: Command-line arguments (default: sys.argv[1:]).

    Returns:
        Exit code (0 on success or KeyboardInterrupt, non-zero on error).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # Build config objects
    llm_cfg = LLMConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
    )
    window = WindowConfig(
        n=args.n,
        overlap=args.overlap,
        gap=args.gap,
        mode=args.mode,
    )

    # Display dashboard URL
    dash_url = f"http://{args.host}:{args.port}/"
    print(f"viewlyt-live -> dashboard: {dash_url}")

    # Display chat popout URL if a video was provided
    if args.url:
        try:
            from ..scraper import extract_video_id

            vid = extract_video_id(args.url)
            print(f"chat popout:  https://www.youtube.com/live_chat?is_popout=1&v={vid}")
        except Exception:
            pass

    # Display snippet URL
    print(f"snippet:      {dash_url}snippet.js  (or copy it from the dashboard)")

    # Launch server (lazy import)
    try:
        from . import server

        server.run(
            host=args.host,
            port=args.port,
            llm_cfg=llm_cfg,
            window=window,
            open_browser=not args.no_open,
        )
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
