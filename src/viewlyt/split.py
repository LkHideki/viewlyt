"""``vl split`` — count tokens in collected ``.md`` and copy budget-sized parts.

Answers "can I paste all of this into one LLM, or must I split it?" and, when
it must be split, walks you through copying each part to the clipboard from a
small interactive terminal menu (no heavy TUI dep — plain stdin + ANSI).

Selenium-free (like ``vl ask``/``vl live``): the only heavy dep is `tiktoken`,
imported lazily via :mod:`viewlyt.tokens`, so ``vl split --help`` works without
the ``tokens`` extra installed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import tokens as tok
from .clipboard import copy_to_clipboard


def _color(text: str, code: str) -> str:
    """ANSI color, only on a real terminal."""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _preview(part: str, width: int = 52) -> str:
    """First meaningful line of a part, de-marked and truncated, for the menu."""
    for raw in part.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line and not line.startswith("<!--"):
            return line[:width] + ("…" if len(line) > width else "")
    return "(vazio)"


def _wrap_part(part: str, i: int, n: int, add_header: bool) -> str:
    """Prepend a fragment marker so the LLM knows it's part i of n."""
    if not add_header:
        return part
    kt = tok.fmt_kt(tok.count_tokens(part))
    return f"<!-- viewlyt — parte {i}/{n} ({kt}) -->\n\n{part}"


def _copy_part(parts: list[str], i: int, n: int, add_header: bool, copied: set[int]) -> None:
    payload = _wrap_part(parts[i], i + 1, n, add_header)
    kt = tok.fmt_kt(tok.count_tokens(parts[i]))
    if copy_to_clipboard(payload):
        copied.add(i)
        print(_color(f"  ✓ parte {i + 1}/{n} copiada ({kt}) — cole na LLM e volte aqui", "32"))
    else:
        print(
            _color("  ✗ não achei pbcopy/clip/xclip/xsel — não deu pra copiar", "31"),
            file=sys.stderr,
        )


def _print_parts(parts: list[str], copied: set[int]) -> None:
    for i, part in enumerate(parts):
        mark = _color("✓", "32") if i in copied else " "
        kt = tok.fmt_kt(tok.count_tokens(part)).rjust(8)
        print(f"  {mark} [{i + 1:>2}] {_color(kt, '36')}  {_preview(part)}")


def _interactive(parts: list[str], add_header: bool) -> None:
    n = len(parts)
    copied: set[int] = set()
    _print_parts(parts, copied)
    hint = _color("nº=copiar · a=todas em sequência · l=listar · q=sair", "90")
    while True:
        try:
            raw = input(f"\n{hint}\n> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not raw:
            continue
        if raw in ("q", "quit", "sair"):
            break
        if raw in ("l", "list", "listar"):
            _print_parts(parts, copied)
            continue
        if raw in ("a", "all", "todas"):
            for i in range(n):
                _copy_part(parts, i, n, add_header, copied)
                if i < n - 1:
                    try:
                        if input(
                            _color("    Enter=próxima · q=parar > ", "90")
                        ).strip().lower() in (
                            "q",
                            "quit",
                        ):
                            break
                    except (EOFError, KeyboardInterrupt):
                        print()
                        return
            continue
        if raw.isdigit() and 1 <= int(raw) <= n:
            _copy_part(parts, int(raw) - 1, n, add_header, copied)
        else:
            print(_color(f"  ? use 1-{n}, 'a', 'l' ou 'q'", "33"))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vl split",
        description="Count tokens in collected .md files and, if they don't fit one prompt, "
        "copy budget-sized parts to the clipboard from an interactive menu.",
    )
    p.add_argument("files", nargs="+", help="one or more .md files (globs expanded by the shell)")
    p.add_argument(
        "-b",
        "--budget",
        type=float,
        default=tok.DEFAULT_BUDGET / 1000,
        metavar="KT",
        help="context budget in thousands of tokens (default: %(default)s kt ≈ a GPT-class "
        "window; Claude Sonnet 5 ≈ 200, Gemini ≈ 1000)",
    )
    p.add_argument(
        "--encoding",
        default=tok.DEFAULT_ENCODING,
        help="tiktoken encoding used to count (default: %(default)s — a proxy; counts are estimates)",
    )
    p.add_argument(
        "--no-part-header",
        action="store_true",
        help="don't prepend a '<!-- viewlyt — parte i/n -->' marker to each copied part",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not tok.tiktoken_available():
        print(
            "vl split needs the tokenizer — install it with:  uv sync --extra tokens",
            file=sys.stderr,
        )
        return 2

    # Read every file; report each one's size, then work on them joined (that's
    # the single thing you'd paste into the model).
    docs: list[tuple[str, str]] = []
    for f in args.files:
        path = Path(f)
        try:
            docs.append((f, path.read_text(encoding="utf-8")))
        except OSError as exc:
            print(_color(f"  ✗ {f}: {exc}", "31"), file=sys.stderr)
    if not docs:
        print("nada pra ler.", file=sys.stderr)
        return 1

    budget_tokens = int(args.budget * 1000)
    enc = args.encoding

    def count(text: str) -> int:
        return tok.count_tokens(text, enc)

    print(_color("=== viewlyt split ===", "1;36"))
    for f, text in docs:
        print(f"  {f}  {_color(tok.fmt_kt(count(text)), '36')}")

    combined = "\n\n".join(text for _f, text in docs)
    total = count(combined)
    print(f"\ntotal: {_color(tok.fmt_kt(total), '1;36')}   |   budget: {tok.fmt_kt(budget_tokens)}")

    if total <= budget_tokens:
        print(_color("✓ cabe inteiro numa LLM — nada a splittar.", "32"))
        parts = [combined]
        # Still offer to copy the whole thing in one go.
    else:
        parts = tok.split_by_budget(combined, budget_tokens, count)
        print(
            _color(
                f"↯ não cabe → {len(parts)} partes (cada uma ≤ {tok.fmt_kt(budget_tokens)})\n",
                "33",
            )
        )

    _interactive(parts, add_header=not args.no_part_header)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
