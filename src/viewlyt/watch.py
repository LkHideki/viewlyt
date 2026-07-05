"""``vl watch`` — queue YouTube URLs from the clipboard, then run the normal batch.

Polls the system clipboard for new YouTube URLs while you browse and copy the
ones you want, so you never alt-tab back to a terminal to paste them one by
one. The queue is persisted to disk (survives a crash/kill -9) and, once you
stop (Ctrl-C, ``--max-count``, ``--timeout``, or a standalone ``--run``), it
dispatches straight into the existing ``vl`` batch pipeline (:mod:`viewlyt.cli`)
with every product flag (``-c``/``-t``/``-r``/``-u``/...) passed through
unchanged — no duplicated pool/retry/tqdm logic.

Selenium-free until that final dispatch: only stdlib + the pure
:mod:`viewlyt.htmltext` helpers (``extract_video_id``,
``looks_like_youtube_reference``) are imported at module load; ``.cli`` is
imported lazily, inside the dispatch itself.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import version as _pkg_version
from pathlib import Path

from .clipboard import read_clipboard
from .htmltext import extract_video_id, looks_like_youtube_reference


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
    """``(video_id, url)`` if ``text`` is a not-yet-seen YouTube URL/id, else ``None``.

    Gated by :func:`looks_like_youtube_reference` first — unlike ``gather_urls``
    (explicit CLI/file input, where a bare 11-char token is presumably meant as
    an id), the clipboard carries whatever the user copies while working, and
    ``extract_video_id``'s last-resort fallback would otherwise turn ordinary
    prose into a bogus queued "video".
    """
    if not text or not looks_like_youtube_reference(text):
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


@contextlib.contextmanager
def _raw_terminal():
    """POSIX: put stdin in raw mode for the WHOLE review session (entered once
    by :func:`_review_tui`, not per-keystroke inside :func:`_read_key`).

    Toggling raw/cooked mode on every single key would reopen a cooked-mode
    window between reads; ``tty.setraw``'s default ``TCSAFLUSH`` discards any
    input "received but not read" at the moment it applies, so a key landing
    in that window (e.g. two arrow-key presses in quick succession, or the
    follow-up bytes of an escape sequence) gets silently eaten by the NEXT
    call. Entering raw mode once for the session's duration shrinks that
    window to a single instant at startup; passing ``TCSANOW`` (instead of the
    default TCSAFLUSH) closes it entirely — the switch never discards
    already-queued input, so even a keystroke that arrives at the very moment
    raw mode is being applied still gets read afterward. No-op on Windows:
    ``msvcrt`` reads single chars without needing a mode change.
    """
    if sys.platform == "win32":
        yield
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd, termios.TCSANOW)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key() -> str:
    """Block for one keypress; return a symbolic name: ``"up"``/``"down"``/
    ``"space"``/``"enter"``/``"quit"``, or ``""`` for anything else.

    Assumes raw mode is already active on POSIX (see :func:`_raw_terminal`) so
    keys arrive unbuffered/un-echoed; a lone ESC (no follow-up bytes within a
    short window) is ``"quit"``, while ``ESC [ A``/``ESC [ B`` (or the
    ``O A``/``O B`` app-mode form) are the arrow keys. Windows: ``msvcrt``
    needs no such setup. Both branches import lazily — ``termios``/``select``
    don't exist on Windows and ``msvcrt`` doesn't exist on POSIX.
    """
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):  # arrow-key prefix
            return {b"H": "up", b"P": "down"}.get(msvcrt.getch(), "")
        if ch in (b"\r", b"\n"):
            return "enter"
        if ch == b" ":
            return "space"
        if ch in (b"j", b"J"):
            return "down"
        if ch in (b"k", b"K"):
            return "up"
        if ch in (b"\x1b", b"q", b"Q"):
            return "quit"
        return ""

    import os
    import select

    fd = sys.stdin.fileno()
    # Read straight off the fd (os.read), never through sys.stdin's buffered
    # wrapper: a buffered read can silently slurp the arrow-key's follow-up
    # bytes into its OWN internal buffer, so the select() below — which only
    # sees the raw fd — finds nothing left and misreads the arrow as a lone
    # ESC ("quit").
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":
        # Lone ESC vs. an arrow-key escape sequence: peek with a short
        # timeout instead of blocking on the 2 extra bytes forever.
        if select.select([fd], [], [], 0.05)[0]:
            rest = os.read(fd, 2).decode(errors="ignore")
            return {"[A": "up", "[B": "down", "OA": "up", "OB": "down"}.get(rest, "quit")
        return "quit"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == " ":
        return "space"
    if ch in ("j", "J"):
        return "down"
    if ch in ("k", "K"):
        return "up"
    if ch in ("q", "Q"):
        return "quit"
    return ""


def _apply_key(key: str, cursor: int, selected: list[bool]) -> tuple[int, str | None]:
    """One pure step of the checkbox TUI. Returns ``(new_cursor, action)``,
    ``action`` being ``None`` (keep looping), ``"run"`` or ``"quit"``. Mutates
    ``selected`` in place on ``"space"``; an unrecognized key is a no-op."""
    n = len(selected)
    if key == "up":
        return (cursor - 1) % n, None
    if key == "down":
        return (cursor + 1) % n, None
    if key == "space":
        selected[cursor] = not selected[cursor]
        return cursor, None
    if key == "enter":
        return cursor, "run"
    if key == "quit":
        return cursor, "quit"
    return cursor, None


def _render_checklist(queue: list[dict], selected: list[bool], cursor: int, redraw: bool) -> None:
    """Draw the checkbox list. Each line is truncated to the terminal width so
    it can never wrap — which would throw off the next redraw's "move the
    cursor up N lines" math (there's no curses here, just plain ANSI).

    Lines are joined/terminated with ``\\r\\n``, never a bare ``\\n``: raw mode
    (see :func:`_raw_terminal`) turns off OPOST, so the tty no longer adds the
    carriage return a bare ``\\n`` gets in cooked mode — without the explicit
    ``\\r`` each line would start where the previous one ended instead of at
    column 0, staircasing the whole display.
    """
    width = shutil.get_terminal_size((100, 24)).columns
    lines = ["fila — espaço=marcar/desmarcar · ↑/↓=navegar · enter=rodar · esc/q=sair"]
    for i, item in enumerate(queue):
        box = "x" if selected[i] else " "
        pointer = "›" if i == cursor else " "
        line = f"{pointer} [{box}] {i + 1:>2}  {item['video_id']:<12}  {item['url']}"
        lines.append(line[: max(width - 1, 10)])
    if redraw:
        sys.stdout.write(f"\x1b[{len(lines)}A\r\x1b[J")
    sys.stdout.write("\r\n".join(lines) + "\r\n")
    sys.stdout.flush()


def _review_tui(queue: list[dict]) -> list[dict] | None:
    """Interactive checkbox review over ``queue`` (all items start checked).

    Space toggles the item under the cursor, arrows/``j``/``k`` move it, Enter
    dispatches the checked items, Esc/``q`` quits without running anything.
    Returns the checked items to run (removing them from ``queue`` in place —
    unchecked ones stay queued for next time), or ``None`` on quit (``queue``
    is left untouched). Requires a real terminal on stdin AND stdout; callers
    must gate on that themselves (raw tty mode needs an actual tty).
    """
    selected = [True] * len(queue)
    cursor = 0
    _render_checklist(queue, selected, cursor, redraw=False)
    with _raw_terminal():
        while True:
            cursor, action = _apply_key(_read_key(), cursor, selected)
            _render_checklist(queue, selected, cursor, redraw=True)
            if action == "quit":
                return None
            if action == "run":
                to_run = [item for item, sel in zip(queue, selected, strict=True) if sel]
                queue[:] = [item for item, sel in zip(queue, selected, strict=True) if not sel]
                return to_run


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

    to_run: list[dict] | None
    if args.yes:
        to_run = list(queue)
        _save_queue(queue_file, [])
    else:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print(
                "vl watch: revisão interativa precisa de um terminal real "
                "— use --yes para despachar direto.",
                file=sys.stderr,
            )
            return 2
        to_run = _review_tui(queue)
        _save_queue(queue_file, queue)  # persist whatever stayed unchecked
        if to_run is None:
            return 0

    if not to_run:
        print("nada selecionado para rodar.")
        return 0

    urls = [item["url"] for item in to_run]

    forwarded = [*passthrough, "--out-dir", args.out_dir]
    if args.quiet:
        forwarded.append("--quiet")

    from .cli import main as cli_main

    return cli_main([*urls, *forwarded])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
