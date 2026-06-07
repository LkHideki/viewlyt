"""Command-line entry point: scrape one or many videos into text files.

Accepts multiple URLs and/or files (`.txt` one-per-line, `.csv` any column) that
list video URLs/ids. Videos are processed by a bounded pool of **reused** Chrome
instances (one driver per worker, amortising browser startup across many videos),
so the I/O-bound work runs in parallel without spawning a browser per URL.

Output per video: ``out/<title-slug>-<video_id>.txt`` — one block per top-level
comment (comment + its replies), blocks separated by a blank line:

    @user [842 likes, 2026-06-04]: message text
        ↳ (in reply to @user) @other [4 likes, 2026-06-03]: a reply

Concurrency note: the HTML->text step uses a batched ``ThreadPoolExecutor``
(benchmarked as the right call vs subinterpreters/processes for many tiny parses).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from queue import Empty, Queue

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


# --------------------------------------------------------------------------- #
# Input gathering: URLs from the CLI and/or .txt / .csv files
# --------------------------------------------------------------------------- #
def read_urls_from_file(path: str) -> list[str]:
    """Return the candidate URL/id strings in a file.

    `.csv` → every non-empty cell of every row (handles arbitrary layouts).
    anything else (`.txt`, …) → each non-empty line that isn't a `#` comment.
    Validity is filtered later via `extract_video_id`.
    """
    p = Path(path)
    out: list[str] = []
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.reader(fh):
                out.extend(cell.strip() for cell in row if cell and cell.strip())
    else:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    out.append(line)
    return out


def gather_urls(inputs: list[str], from_files: list[str]) -> list[tuple[str, str]]:
    """Expand CLI inputs + `--from-file` into a de-duplicated, order-preserving
    list of ``(video_id, url)``.

    Each positional input that is an existing file is read as a list of URLs;
    otherwise it is treated as a URL/id. Items that don't parse to a video id are
    skipped with a warning. Duplicate video ids are dropped.
    """
    candidates: list[str] = []
    for item in inputs or []:
        if Path(item).is_file():
            candidates.extend(read_urls_from_file(item))
        else:
            candidates.append(item)
    for f in from_files or []:
        candidates.extend(read_urls_from_file(f))

    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for raw in candidates:
        try:
            vid = extract_video_id(raw)
        except ValueError:
            log.warning("ignoring (not a YouTube URL/id): %r", raw)
            continue
        if vid in seen:
            continue
        seen.add(vid)
        result.append((vid, raw))
    return result


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def build_primed_driver(headless: bool, user_data_dir: str | None):
    """Build a driver and prime consent cookies ONCE; reuse it for many videos."""
    driver = build_driver(headless=headless, user_data_dir=user_data_dir)
    prime_consent_cookies(driver)
    return driver


def scrape_one(
    driver,
    url: str,
    *,
    limit: int,
    max_viewports: int,
    expand_replies: bool,
    max_replies: int,
    progress: bool,
) -> tuple[str, str, list[dict]]:
    """Scrape a single video with an already-primed driver. Raises
    ``BlockedError`` on a consent/bot wall."""
    video_id = extract_video_id(url)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("video id: %s", video_id)
    safe_get(driver, watch_url)
    dismiss_consent_dialog(driver, timeout=2.0)

    block = detect_block(driver)
    if block:
        raise BlockedError(block)

    title = get_video_title(driver)
    records = collect_comments(
        driver, limit=limit, max_viewports=max_viewports,
        expand_replies=expand_replies, max_replies=max_replies, progress=progress,
    )
    return video_id, title, records


# --------------------------------------------------------------------------- #
# Formatting / output
# --------------------------------------------------------------------------- #
def _convert_all(htmls: list[str], progress: bool = True) -> list[str]:
    """HTML -> text for every fragment, in order, via a batched ThreadPoolExecutor."""
    if not htmls:
        return []
    size = 64
    chunks = [htmls[i:i + size] for i in range(0, len(htmls), size)]
    results: list[list[str]] = [[] for _ in chunks]
    workers = min(8, (os.cpu_count() or 4))
    with tqdm(total=len(htmls), desc="parsing comments", unit="cmt", leave=False, disable=not progress) as bar:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_idx = {ex.submit(convert_batch, ch): i for i, ch in enumerate(chunks)}
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                results[i] = fut.result()
                bar.update(len(chunks[i]))
    return [text for batch in results for text in batch]


def format_comment_lines(records: list[dict], today: date | None = None, progress: bool = True) -> list[str]:
    """Render records as text lines grouped into blocks (a top-level comment and
    its replies), with a blank line between blocks. Replies are indented and name
    their parent."""
    if not records:
        return []
    today = today or date.today()

    texts = _convert_all([r.get("html", "") for r in records], progress=progress)

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
    out_path = Path(out_dir) / f"{slug or 'video'}-{video_id}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Batch runner: bounded pool of reused browser instances
# --------------------------------------------------------------------------- #
def run_batch(targets: list[tuple[str, str]], *, jobs: int, headless: bool, fallback: bool,
              user_data_dir: str | None, out_dir: str, limit: int, max_viewports: int,
              expand_replies: bool, max_replies: int, inner_progress: bool, quiet: bool) -> list[dict]:
    """Process every (video_id, url) using ``jobs`` worker threads, each owning a
    reused, primed driver. Failures are isolated per-video; a poisoned session is
    recycled. Returns a summary dict per target."""
    q: "Queue[tuple[str, str]]" = Queue()
    for t in targets:
        q.put(t)

    summaries: list[dict] = []
    s_lock = threading.Lock()
    bar = tqdm(total=len(targets), desc="vídeos", unit="vídeo",
               disable=(len(targets) == 1 or quiet))
    bar_lock = threading.Lock()

    scrape_kw = dict(limit=limit, max_viewports=max_viewports,
                     expand_replies=expand_replies, max_replies=max_replies,
                     progress=inner_progress)

    def add(summary: dict) -> None:
        with s_lock:
            summaries.append(summary)
        with bar_lock:
            bar.update(1)

    def worker(worker_id: int) -> None:
        local_headless = headless
        driver = None
        try:
            while True:
                try:
                    video_id, url = q.get_nowait()
                except Empty:
                    break
                try:
                    if driver is None:
                        driver = build_primed_driver(local_headless, user_data_dir)
                    try:
                        vid, title, records = scrape_one(driver, url, **scrape_kw)
                    except BlockedError as exc:
                        if local_headless and fallback:
                            log.warning("[w%d] blocked (%s) on %s — switching this worker to headed",
                                        worker_id, exc.kind, video_id)
                            _safe_quit(driver)
                            local_headless = False
                            driver = build_primed_driver(local_headless, user_data_dir)
                            vid, title, records = scrape_one(driver, url, **scrape_kw)
                        else:
                            raise
                    lines = format_comment_lines(records, progress=inner_progress)
                    path = _write(slugify(title), vid, lines, out_dir)
                    n_top = sum(1 for r in records if r.get("kind") == "comment")
                    add({"url": url, "video_id": vid, "title": title, "file": str(path),
                         "comments": n_top, "lines": len(lines), "error": None})
                except Exception as exc:  # isolate this video; recycle the session
                    add({"url": url, "video_id": video_id, "title": None, "file": None,
                         "comments": 0, "lines": 0, "error": str(exc) or type(exc).__name__})
                    _safe_quit(driver)
                    driver = None
                finally:
                    q.task_done()
        finally:
            _safe_quit(driver)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    bar.close()
    # Restore input order in the summary.
    order = {vid: i for i, (vid, _u) in enumerate(targets)}
    summaries.sort(key=lambda s: order.get(s["video_id"], 1 << 30))
    return summaries


def _safe_quit(driver) -> None:
    if driver is not None:
        try:
            driver.quit()
        except Exception:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ytcomments",
        description="Scrape YouTube comments (likes, dates, replies) into "
        "out/<title-slug>-<video_id>.txt. Accepts many URLs and/or .txt/.csv files.",
    )
    p.add_argument("inputs", nargs="*",
                   help="one or more video URLs/ids, and/or paths to .txt/.csv files listing them")
    p.add_argument("-f", "--from-file", action="append", default=[], metavar="PATH",
                   help="file (.txt one-per-line, or .csv any column) with video URLs/ids; repeatable")
    p.add_argument("-j", "--jobs", type=int, default=None,
                   help="number of concurrent browser instances (default: min(4, nº de vídeos))")
    p.add_argument("--limit", type=int, default=100,
                   help="target top-level comments per video, or all if fewer (default: 100)")
    p.add_argument("--max-viewports", type=int, default=25,
                   help="scroll budget per video (scroll-to-bottom steps, default: 25)")
    p.add_argument("--no-replies", action="store_true", help="don't expand/collect replies (faster)")
    p.add_argument("--max-replies", type=int, default=10,
                   help="max replies per comment (default: 10; 0 disables)")
    p.add_argument("--headed", action="store_true",
                   help="visible browser instead of headless (more reliable vs the bot wall)")
    p.add_argument("--no-fallback", action="store_true",
                   help="don't auto-retry headed when a block is detected")
    p.add_argument("--user-data-dir", default=None,
                   help="persistent Chrome profile dir (a signed-in profile defeats the bot wall)")
    p.add_argument("-o", "--out-dir", default="out",
                   help="directory for <title-slug>-<video_id>.txt (default: out)")
    p.add_argument("-q", "--quiet", action="store_true", help="only log warnings/errors")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        targets = gather_urls(args.inputs, args.from_file)
    except OSError as exc:
        log.error("could not read input file: %s", exc)
        return 2
    if not targets:
        log.error("no valid YouTube URLs/ids given (pass URLs and/or --from-file a .txt/.csv)")
        return 2

    jobs = args.jobs if args.jobs and args.jobs > 0 else min(4, len(targets))
    jobs = max(1, min(jobs, len(targets)))
    inner_progress = (len(targets) == 1 and not args.quiet)

    log.info("%d video(s) to scrape with %d browser instance(s)", len(targets), jobs)
    summaries = run_batch(
        targets, jobs=jobs, headless=not args.headed, fallback=not args.no_fallback,
        user_data_dir=args.user_data_dir, out_dir=args.out_dir, limit=args.limit,
        max_viewports=args.max_viewports, expand_replies=not args.no_replies,
        max_replies=args.max_replies, inner_progress=inner_progress, quiet=args.quiet,
    )

    ok = [s for s in summaries if not s["error"]]
    failed = [s for s in summaries if s["error"]]
    print(f"\nDone: {len(ok)}/{len(summaries)} video(s) scraped"
          + (f", {len(failed)} failed" if failed else ""))
    for s in ok:
        print(f"  ✓ {s['video_id']}  {s['comments']} comments, {s['lines']} lines -> {s['file']}")
    for s in failed:
        print(f"  ✗ {s['video_id']}  {s['error']}", file=sys.stderr)
    if failed and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
