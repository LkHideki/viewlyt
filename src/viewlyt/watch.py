"""``vl watch`` — queue YouTube URLs from the clipboard, then run the normal batch.

Polls the system clipboard for new YouTube URLs while you browse and copy the
ones you want, so you never alt-tab back to a terminal to paste them one by
one. The queue is persisted to disk (survives a crash/kill -9) and, once you
stop (Ctrl-C, ``--max-count``, ``--timeout``, or a standalone ``--run``), it
dispatches straight into the existing ``vl`` batch pipeline (:mod:`viewlyt.cli`)
with every product flag (``-c``/``-t``/``-r``/``-u``/...) passed through
unchanged — no duplicated pool/retry/tqdm logic.

Selenium-free until that final dispatch: only stdlib + the pure
:func:`viewlyt.htmltext.extract_video_id` are imported at module load; ``.cli``
is imported lazily, inside the dispatch itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path

from .clipboard import read_clipboard
from .htmltext import extract_video_id


def _default_queue_file(out_dir: str) -> Path:
    return Path(out_dir) / ".watch" / "queue.json"


def _load_queue(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _save_queue(path: Path, queue: list[dict]) -> None:
    """Write ``queue`` atomically (tmp file + replace) so a kill -9 mid-write
    never corrupts the previously-persisted queue — only the item in flight
    is ever at risk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _check_new_url(text: str | None, seen: set[str]) -> tuple[str, str] | None:
    """``(video_id, url)`` if ``text`` is a not-yet-seen YouTube URL/id, else ``None``."""
    if not text:
        return None
    try:
        video_id = extract_video_id(text)
    except ValueError:
        return None
    if video_id in seen:
        return None
    return video_id, text


def _poll_once(text: str | None, seen: set[str], queue: list[dict]) -> bool:
    """Append a new hit to ``queue``/``seen`` in place; return whether it changed."""
    hit = _check_new_url(text, seen)
    if hit is None:
        return False
    video_id, url = hit
    seen.add(video_id)
    queue.append(
        {
            "video_id": video_id,
            "url": url,
            "added_at": datetime.now(UTC).isoformat(),
        }
    )
    return True


def _print_queue(queue: list[dict]) -> None:
    if not queue:
        print("fila vazia.")
        return
    print(f"fila: {len(queue)} item(ns)")
    for i, item in enumerate(queue, start=1):
        print(f"  [{i}] {item['video_id']}  {item['url']}")


def _review_prompt(queue: list[dict], queue_file: Path) -> str:
    """Synchronous (no thread) post-poll review: list/undo/run/quit. Returns
    ``"run"`` or ``"quit"``."""
    _print_queue(queue)
    hint = "l=listar · u=undo (remove o último) · r=rodar · q=sair"
    while True:
        try:
            raw = input(f"\n{hint}\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "quit"
        if not raw:
            continue
        if raw in ("l", "list", "listar"):
            _print_queue(queue)
        elif raw in ("u", "undo", "remover"):
            if not queue:
                print("fila já está vazia.")
                continue
            removed = queue.pop()
            _save_queue(queue_file, queue)
            print(f"removido: {removed['video_id']}  {removed['url']}")
        elif raw in ("r", "run", "rodar"):
            return "run"
        elif raw in ("q", "quit", "sair"):
            return "quit"
        else:
            print("? use l, u, r ou q")


def _run_daemon(args: argparse.Namespace, queue_file: Path) -> list[dict]:
    """Poll the clipboard until Ctrl-C/--max-count/--timeout; return the queue.

    Raises ``RuntimeError`` (propagated by the first :func:`read_clipboard` call,
    used here as an up-front probe too) when no OS clipboard-read tool exists at
    all — so the caller never spins silently forever.
    """
    queue = _load_queue(queue_file)
    seen = {item["video_id"] for item in queue}

    def _accept(text: str | None) -> None:
        if _poll_once(text, seen, queue):
            _save_queue(queue_file, queue)
            if not args.quiet:
                print(f"+1 (total {len(queue)}): {queue[-1]['url']}")

    last_text = read_clipboard()  # also the up-front "a tool exists" probe
    _accept(last_text)

    start = time.monotonic()
    try:
        while True:
            if (args.max_count and len(queue) >= args.max_count) or (
                args.timeout and (time.monotonic() - start) >= args.timeout
            ):
                break
            time.sleep(args.poll_interval)
            text = read_clipboard()
            if text is not None and text != last_text:
                last_text = text
                _accept(text)
    except KeyboardInterrupt:
        print()
    return queue


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vl watch",
        description="Watch the clipboard for YouTube URLs, queue them as you browse, and "
        "dispatch the same 'vl' batch scrape once you're done (Ctrl-C / --run). Any flag "
        "vl itself accepts (-c/-t/-r/-u/--limit-comments/...) is passed straight through.",
    )
    p.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {_pkg_version('viewlyt')}"
    )
    p.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="clipboard poll interval (default: 1.0)",
    )
    p.add_argument(
        "--queue-file",
        default=None,
        metavar="PATH",
        help="override the persisted queue file (default: <out-dir>/.watch/queue.json)",
    )
    p.add_argument(
        "--run",
        action="store_true",
        help="skip polling: dispatch the already-saved queue right now",
    )
    p.add_argument("--list", action="store_true", help="print the saved queue and exit")
    p.add_argument(
        "--drop-last",
        action="store_true",
        help="remove the last item from the saved queue and exit",
    )
    p.add_argument(
        "--max-count",
        type=int,
        default=0,
        metavar="N",
        help="stop polling once the queue reaches N items (0 = no limit)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="stop polling after this many seconds total (0 = no limit)",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the post-poll review prompt and dispatch immediately "
        "(required for unattended/background use with --max-count/--timeout)",
    )
    p.add_argument(
        "-o",
        "--out-dir",
        default="out",
        help="anchors the default queue file and is forwarded to the scrape (default: out)",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the '+1 (total N): <url>' line per accepted URL (also forwarded)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args, passthrough = build_parser().parse_known_args(argv)
    queue_file = Path(args.queue_file) if args.queue_file else _default_queue_file(args.out_dir)

    if args.list:
        _print_queue(_load_queue(queue_file))
        return 0

    if args.drop_last:
        queue = _load_queue(queue_file)
        if not queue:
            print("fila já está vazia — nada para remover.", file=sys.stderr)
            return 0
        removed = queue.pop()
        _save_queue(queue_file, queue)
        print(f"removido: {removed['video_id']}  {removed['url']}")
        return 0

    if args.run:
        queue = _load_queue(queue_file)
    else:
        try:
            queue = _run_daemon(args, queue_file)
        except RuntimeError as exc:
            print(f"vl watch: {exc}", file=sys.stderr)
            return 2

    if not queue:
        print("fila vazia — nada para rodar.")
        return 0

    if not args.yes:
        if _review_prompt(queue, queue_file) != "run":
            return 0
        if not queue:  # undo(s) during review emptied it
            print("fila vazia — nada para rodar.")
            return 0

    urls = [item["url"] for item in queue]
    _save_queue(queue_file, [])

    forwarded = [*passthrough, "--out-dir", args.out_dir]
    if args.quiet:
        forwarded.append("--quiet")

    from .cli import main as cli_main

    return cli_main([*urls, *forwarded])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
