"""Command-line entry point: scrape a video and write its comments.

Output: ``out/<title-slug>-<video_id>.txt`` — comments grouped into blocks
(a comment + its replies), blocks separated by a blank line:

    @user [842 likes, 2026-06-04]: message text
        ↳ (in reply to @user) @other [4 likes, 2026-06-03]: a reply

    @next [42 likes, 2026-06-01]: the next top-level comment

Pipeline:
  build driver -> prime consent cookies -> open watch page -> dismiss consent
  -> detect blocks -> read title -> load (Phase A) + expand & harvest (Phase B)
  records (single-threaded Selenium) -> ThreadPoolExecutor: HTML -> plain text
  -> format lines -> write the file.

Concurrency note: the HTML->text step is the only parallelisable work and is
tiny next to the Selenium phase, so it uses a batched ``ThreadPoolExecutor`` —
benchmarked as the right call here (subinterpreters/processes add more overhead
than they save for many small parses). On a free-threaded build (``python3.14t``)
those threads run truly in parallel for free.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from tqdm import tqdm

from .driver import build_driver
from .htmltext import convert_batch, flatten_inline, parse_relative_date, slugify
from .scraper import (
    BlockedError,
    collect_comments,
    detect_block,
    dismiss_consent_dialog,
    extract_video_id,
    get_video_title,
    prime_consent_cookies,
    safe_get,
)

log = logging.getLogger("ytcomments")

REPLY_INDENT = "    ↳ "  # 4 spaces + ↳


def scrape(
    url: str,
    headless: bool = True,
    user_data_dir: str | None = None,
    max_viewports: int = 25,
    limit: int = 100,
    expand_replies: bool = True,
    max_replies: int = 10,
) -> tuple[str, str, list[dict]]:
    """Return ``(video_id, title, records)``. Raises ``BlockedError`` if YouTube
    serves a consent/bot wall instead of the watch page."""
    video_id = extract_video_id(url)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("video id: %s", video_id)

    driver = build_driver(headless=headless, user_data_dir=user_data_dir)
    try:
        prime_consent_cookies(driver)
        log.info("opening %s", watch_url)
        safe_get(driver, watch_url)
        dismiss_consent_dialog(driver)

        block = detect_block(driver)
        if block:
            raise BlockedError(block)

        title = get_video_title(driver)
        log.info("title: %s", title or "(unknown)")
        records = collect_comments(
            driver, limit=limit, max_viewports=max_viewports,
            expand_replies=expand_replies, max_replies=max_replies,
        )
    finally:
        try:
            driver.quit()
        except Exception:  # pragma: no cover
            pass

    return video_id, title, records


def _convert_all(htmls: list[str]) -> list[str]:
    """HTML -> text for every fragment, in order, via a batched ThreadPoolExecutor
    with a tqdm progress bar. Batching keeps thread-dispatch overhead low; on a
    free-threaded interpreter these threads parallelise for real."""
    if not htmls:
        return []
    size = 64
    chunks = [htmls[i:i + size] for i in range(0, len(htmls), size)]
    results: list[list[str]] = [[] for _ in chunks]
    workers = min(8, (os.cpu_count() or 4))
    with tqdm(total=len(htmls), desc="parsing comments", unit="cmt", leave=False) as bar:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_idx = {ex.submit(convert_batch, ch): i for i, ch in enumerate(chunks)}
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                results[i] = fut.result()
                bar.update(len(chunks[i]))
    return [text for batch in results for text in batch]


def format_comment_lines(records: list[dict], today: date | None = None) -> list[str]:
    """Render records as text lines grouped into blocks (a top-level comment and
    its replies), with a blank line between blocks. Replies are indented and name
    their parent::

        @user [842 likes, 2026-06-04]: message
            ↳ (in reply to @user) @other [4 likes, 2026-06-03]: a reply

        @next [42 likes, 2026-06-01]: ...

    The message is flattened to a single line; dates are approximate (see
    :func:`ytcomments.htmltext.parse_relative_date`).
    """
    if not records:
        return []
    today = today or date.today()

    texts = _convert_all([r.get("html", "") for r in records])

    blocks: list[list[str]] = []
    current: list[str] = []
    seen: set[str] = set()
    for r, text in zip(records, texts):
        message = flatten_inline(text)
        author = r.get("author") or "unknown"
        likes = r.get("likes") or "0"
        when = parse_relative_date(r.get("date_raw", ""), today) or "unknown"

        if r.get("kind") == "reply":
            if not message:
                continue
            parent = r.get("parent_author") or "unknown"
            line = f"{REPLY_INDENT}(in reply to {parent}) {author} [{likes} likes, {when}]: {message}"
            if line in seen:  # belt-and-suspenders against any repeated element
                continue
            seen.add(line)
            current.append(line)
        else:
            # comment boundary: flush the previous block, then start a fresh one
            # (always reset so replies never leak into the previous comment)
            if current:
                blocks.append(current)
            current = []
            if not message:
                continue
            line = f"{author} [{likes} likes, {when}]: {message}"
            seen.add(line)
            current.append(line)
    if current:
        blocks.append(current)

    out: list[str] = []
    for i, block in enumerate(blocks):
        if i:
            out.append("")  # blank line separating blocks
        out.extend(block)
    return out


def _write(slug: str, video_id: str, lines: list[str], out_dir: str) -> Path:
    out_path = Path(out_dir) / f"{slug}-{video_id}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytcomments",
        description="Scrape a YouTube video's comments (with likes, dates and replies) "
        "into out/<title-slug>-<video_id>.txt.",
    )
    p.add_argument("url", help="YouTube video URL (watch?v=, youtu.be/, /shorts/, ...) or bare id")
    p.add_argument(
        "--limit",
        type=int,
        default=100,
        help="target number of top-level comments to collect, or all if fewer (default: 100)",
    )
    p.add_argument(
        "--max-viewports",
        type=int,
        default=25,
        help="scroll budget as a number of scroll-to-bottom steps (default: 25)",
    )
    p.add_argument(
        "--no-replies",
        action="store_true",
        help="do not expand or collect replies (faster)",
    )
    p.add_argument(
        "--max-replies",
        type=int,
        default=10,
        help="max replies to collect per comment (default: 10; 0 disables replies)",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="run with a visible browser window (default: headless). Headed is more reliable against the bot wall.",
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="do not auto-retry in headed mode when a block is detected",
    )
    p.add_argument(
        "--user-data-dir",
        default=None,
        help="persistent Chrome profile dir (use a once-logged-in profile to defeat the bot wall on flagged IPs)",
    )
    p.add_argument(
        "-o",
        "--out-dir",
        default="out",
        help="directory to write <title-slug>-<video_id>.txt into (default: out)",
    )
    p.add_argument("-q", "--quiet", action="store_true", help="only log warnings/errors")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    headless = not args.headed
    kwargs = dict(
        user_data_dir=args.user_data_dir,
        max_viewports=args.max_viewports,
        limit=args.limit,
        expand_replies=not args.no_replies,
        max_replies=args.max_replies,
    )
    try:
        try:
            video_id, title, records = scrape(args.url, headless=headless, **kwargs)
        except BlockedError as exc:
            if headless and not args.no_fallback:
                log.warning("blocked (%s) in headless mode — retrying with a visible browser…", exc.kind)
                video_id, title, records = scrape(args.url, headless=False, **kwargs)
            else:
                raise
    except BlockedError as exc:
        log.error(
            "YouTube blocked the request (%s). Try: --headed, or --user-data-dir with a "
            "Chrome profile already signed in to YouTube.",
            exc.kind,
        )
        return 2
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    except Exception as exc:  # pragma: no cover - surface driver/setup failures
        log.error("scrape failed: %s", exc)
        return 1

    lines = format_comment_lines(records)
    slug = slugify(title)
    out_path = _write(slug, video_id, lines, args.out_dir)
    print(f"Saved {len(lines)} lines to {out_path}")
    if not lines:
        print(
            "(0 comments — the video may have comments disabled, or a block was hit. "
            "Try --headed or --user-data-dir.)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
