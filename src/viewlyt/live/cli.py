"""Command-line entry point for viewlyt-live (real-time YouTube live-chat analysis).

Launches a FastAPI dashboard on --host:--port, optionally auto-opens it in a browser,
and displays URLs for the YouTube popout chat and the browser snippet. Imports the
server module lazily so FastAPI/uvicorn only load at run time.
"""

from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version

from .llm import LLMConfig, provider_base_url
from .window import WindowConfig


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``vl live``."""
    parser = argparse.ArgumentParser(
        prog="vl live",
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
        "--provider",
        choices=["lmstudio", "ollama", "openai", "openrouter", "groq"],
        default="openrouter",
        help="LLM provider (sets the base URL; --base-url overrides) (default: %(default)s)",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:1234/v1",
        help="LLM base URL (default: sentinel for provider-based resolution)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="LLM API key (default: empty; required for cloud providers)",
    )
    parser.add_argument(
        "--model",
        default="google/gemini-3.1-flash-lite",
        help="LLM model name (default: %(default)s)",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=230,
        help="Window size (number of messages, default: %(default)s)",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=0,
        help="Window overlap (default: %(default)s)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=45.0,
        help="Refresh interval in seconds (time/hybrid modes, default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["count", "time", "hybrid"],
        default="hybrid",
        help="Snapshot policy (default: %(default)s)",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=3000,
        help="Max messages kept in the rolling sample buffer (default: %(default)s)",
    )
    parser.add_argument(
        "--capture",
        choices=["browser", "server"],
        default="browser",
        help=(
            "How chat messages reach the server: 'browser' = you run the snippet/"
            "extension in the YouTube popout (default); 'server' = this process "
            "drives its own headless Chrome on the popout — nothing to paste, and "
            "the ONLY option that works with Safari (default: %(default)s)"
        ),
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

    # Server-side capture needs a target — fail fast instead of booting a
    # dashboard that will never see messages. (Chrome itself is checked at
    # runtime by the capture thread, with a logged, self-retrying error.)
    if args.capture == "server" and not args.url:
        parser.error("--capture server needs the live URL/id as the positional argument")

    # Resolve base_url: use explicit --base-url if different from default, else use provider
    default_base_url = "http://localhost:1234/v1"
    resolved_base_url = (
        args.base_url if args.base_url != default_base_url else provider_base_url(args.provider)
    )

    # Build config objects
    llm_cfg = LLMConfig(
        base_url=resolved_base_url,
        api_key=args.api_key,
        model=args.model,
    )
    window = WindowConfig(
        n=args.n,
        overlap=args.overlap,
        gap=args.gap,
        mode=args.mode,
        capacity=args.capacity,
    )

    # Display dashboard URL
    dash_url = f"http://{args.host}:{args.port}/"
    print(f"vl live -> dashboard: {dash_url}")

    if args.capture == "server":
        print("capture:      server-side (chat pulled by this process — no snippet needed)")
    else:
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
            capture_url=args.url if args.capture == "server" else None,
        )
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
