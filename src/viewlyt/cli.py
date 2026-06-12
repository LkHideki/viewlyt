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
from importlib.metadata import version as _pkg_version
from pathlib import Path
from queue import Empty, Queue

from tqdm import tqdm

from .driver import build_driver
from .htmltext import (
    REPLY_INDENT,
    convert_batch,
    format_related,
    format_transcript,
    format_unified,
    join_unified,
    slugify,
)
from .htmltext import format_comment_lines as _format_comment_lines
from .scraper import (
    BlockedError,
    collect_comments,
    collect_related,
    detect_block,
    dismiss_consent_dialog,
    extract_video_id,
    fetch_transcript,
    get_video_title,
    prime_consent_cookies,
    safe_get,
)

log = logging.getLogger("viewlyt")

# When --unify/--unify-all is given with NO product selector, collect everything;
# related needs a count, so default to this (overridable with -r N).
_UNIFY_DEFAULT_RELATED = 20

UNIFIED_ALL_FILENAME = "unified-all.txt"  # the single --unify-all output

_EXAMPLES = """\
exemplos:
  viewlyt 'https://youtu.be/dQw4w9WgXcQ'          # comentários -> out/<slug>-<id>.txt
  viewlyt -c -t '<url>'                            # comentários + transcrição
  viewlyt -t '<url>'                               # só a transcrição (mais rápido)
  viewlyt --transcript-only '<url>'                # idem (alias de -t sem -c)
  viewlyt --no-merge-comments '<url>'              # não funde comentários do mesmo autor
  viewlyt --limit-comments 50 --no-replies '<url>' # coleta enxuta e rápida
  viewlyt --unify '<url>'                          # todos os produtos num só arquivo
  viewlyt --unify-all '<url1>' '<url2>'            # todos os vídeos num arquivo só
  viewlyt --from-file urls.txt -j 4                # vários vídeos (.txt/.csv), 4 navegadores
  viewlyt --headed '<url>'                         # navegador visível (contra o bot wall)
"""


def _color(text: str, code: str) -> str:
    """Wrap text in an ANSI color only when writing to a real terminal."""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


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
    with_comments: bool = True,
    with_transcript: bool = False,
    with_related: bool = False,
    related_limit: int = 0,
) -> tuple[str, str, list[dict], list[tuple[str, str]], list[dict]]:
    """Scrape a single video with an already-primed driver. Raises
    ``BlockedError`` on a consent/bot wall. Returns
    ``(video_id, title, records, transcript, related)``."""
    video_id = extract_video_id(url)
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    log.info("video id: %s", video_id)
    safe_get(driver, watch_url)
    dismiss_consent_dialog(driver, timeout=2.0)

    block = detect_block(driver)
    if block:
        raise BlockedError(block)

    title = get_video_title(driver)
    records = (
        collect_comments(
            driver,
            limit=limit,
            max_viewports=max_viewports,
            expand_replies=expand_replies,
            max_replies=max_replies,
            progress=progress,
        )
        if with_comments
        else []
    )
    # Related runs AFTER comments (it scrolls back to the top sidebar) but BEFORE
    # the transcript: opening the transcript panel takes over the #secondary column,
    # which would hide the related lockups. collect_related never raises ([] on error).
    related = (
        collect_related(driver, limit=related_limit, progress=progress) if with_related else []
    )
    # Transcript is the LAST page action so its panel/scroll can't perturb the
    # comment lazy-load; fetch_transcript never raises (returns [] on any issue).
    transcript = fetch_transcript(driver, progress=progress) if with_transcript else []
    return video_id, title, records, transcript, related


# --------------------------------------------------------------------------- #
# Mode resolution
# --------------------------------------------------------------------------- #
def resolve_modes(
    comments: bool, transcript: bool, transcript_only: bool, related: int = 0
) -> tuple[bool, bool, bool]:
    """Resolve ``(with_comments, with_transcript, with_related)`` from the selectors.

    ``-c/--comments``, ``-t/--transcript`` and ``-r/--related N`` are independent
    toggles (``related`` is the count; ``> 0`` enables it), with the back-compat
    ``--transcript-only`` alias. Comments are the implicit default ONLY when no
    other selector is given, mirroring how ``-t`` alone means transcript-only:
    no flags -> comments; ``-c`` -> comments; ``-t`` -> transcript; ``-r N`` ->
    related; any combination selects exactly those; ``--transcript-only`` forces
    comments off regardless of ``-c``.
    """
    with_transcript = transcript or transcript_only
    with_related = related > 0
    if transcript_only:
        with_comments = False
    else:
        with_comments = comments or not (with_transcript or with_related)
    return with_comments, with_transcript, with_related


# --------------------------------------------------------------------------- #
# Formatting / output
# --------------------------------------------------------------------------- #
def _convert_all(htmls: list[str], progress: bool = True) -> list[str]:
    """HTML -> text for every fragment, in order, via a batched ThreadPoolExecutor."""
    if not htmls:
        return []
    size = 64
    chunks = [htmls[i : i + size] for i in range(0, len(htmls), size)]
    results: list[list[str]] = [[] for _ in chunks]
    workers = min(8, (os.cpu_count() or 4))
    with tqdm(
        total=len(htmls), desc="parsing comments", unit="cmt", leave=False, disable=not progress
    ) as bar:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_idx = {ex.submit(convert_batch, ch): i for i, ch in enumerate(chunks)}
            for fut in as_completed(fut_to_idx):
                i = fut_to_idx[fut]
                results[i] = fut.result()
                bar.update(len(chunks[i]))
    return [text for batch in results for text in batch]


def format_comment_lines(
    records: list[dict],
    today: date | None = None,
    progress: bool = True,
    merge_comments: bool = True,
) -> list[str]:
    """Thin wrapper over :func:`viewlyt.htmltext.format_comment_lines` that injects
    the batched ``ThreadPoolExecutor`` converter (for the parsing progress bar).

    The output is byte-for-byte identical to the pure formatter — this keeps the
    CLI's back-compat signature (positional ``today``/``progress``) and its
    progress bar while the merge/format logic lives, dependency-free, in
    :mod:`viewlyt.htmltext`."""
    return _format_comment_lines(
        records,
        today=today,
        merge=merge_comments,
        convert=lambda htmls: _convert_all(htmls, progress=progress),
    )


def _write(slug: str, video_id: str, lines: list[str], out_dir: str, suffix: str = "") -> Path:
    out_path = Path(out_dir) / f"{slug or 'video'}-{video_id}{suffix}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------- #
# Batch runner: bounded pool of reused browser instances
# --------------------------------------------------------------------------- #
def run_batch(
    targets: list[tuple[str, str]],
    *,
    jobs: int,
    headless: bool,
    fallback: bool,
    user_data_dir: str | None,
    out_dir: str,
    limit: int,
    max_viewports: int,
    expand_replies: bool,
    max_replies: int,
    with_comments: bool,
    with_transcript: bool,
    with_related: bool,
    related_limit: int,
    merge_comments: bool,
    unify: bool,
    unify_all: bool,
    inner_progress: bool,
    quiet: bool,
) -> list[dict]:
    """Process every (video_id, url) using ``jobs`` worker threads, each owning a
    reused, primed driver. Failures are isolated per-video; a poisoned session is
    recycled. Returns a summary dict per target.

    Output modes: by default one file per product; ``unify`` writes one
    ``<slug>-<id>.unified.txt`` per video instead; ``unify_all`` writes a single
    ``unified-all.txt`` combining every video (the per-video files are skipped)."""
    q: Queue[tuple[str, str]] = Queue()
    for t in targets:
        q.put(t)

    summaries: list[dict] = []
    # --unify-all: each worker stashes its per-video unified block here; the whole
    # document is joined and written once, in input order, after the pool drains.
    unified_blocks: dict[str, list[str]] = {}
    ub_lock = threading.Lock()
    s_lock = threading.Lock()
    bar = tqdm(
        total=len(targets), desc="vídeos", unit="vídeo", disable=(len(targets) == 1 or quiet)
    )
    bar_lock = threading.Lock()

    scrape_kw = dict(
        limit=limit,
        max_viewports=max_viewports,
        expand_replies=expand_replies,
        max_replies=max_replies,
        progress=inner_progress,
        with_comments=with_comments,
        with_transcript=with_transcript,
        with_related=with_related,
        related_limit=related_limit,
    )

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
                        vid, title, records, transcript, related = scrape_one(
                            driver, url, **scrape_kw
                        )
                    except BlockedError as exc:
                        if local_headless and fallback:
                            log.warning(
                                "[w%d] blocked (%s) on %s — switching this worker to headed",
                                worker_id,
                                exc.kind,
                                video_id,
                            )
                            _safe_quit(driver)
                            local_headless = False
                            driver = build_primed_driver(local_headless, user_data_dir)
                            vid, title, records, transcript, related = scrape_one(
                                driver, url, **scrape_kw
                            )
                        else:
                            raise
                    slug = slugify(title)
                    # Format each selected product once; reused by every output mode.
                    clines = (
                        format_comment_lines(
                            records, progress=inner_progress, merge_comments=merge_comments
                        )
                        if with_comments
                        else []
                    )
                    tlines = (
                        format_transcript(transcript) if (with_transcript and transcript) else []
                    )
                    rlines = format_related(related) if (with_related and related) else []
                    # Count rendered top-level blocks (a non-blank line that isn't an
                    # indented reply), so the summary matches the file after merging.
                    n_top = sum(1 for ln in clines if ln and not ln.startswith(REPLY_INDENT))
                    n_lines, n_seg, n_related = len(clines), len(tlines), len(rlines)

                    comment_file = transcript_file = related_file = unified_file = None
                    if unify or unify_all:
                        block = format_unified(
                            title,
                            [
                                ("Comments", clines),
                                ("Transcript", tlines),
                                ("Related videos", rlines),
                            ],
                        )
                        if unify_all:
                            with ub_lock:
                                unified_blocks[vid] = block
                        elif block:  # --unify: one unified file per video
                            unified_file = str(_write(slug, vid, block, out_dir, suffix=".unified"))
                    else:
                        if with_comments:
                            comment_file = str(_write(slug, vid, clines, out_dir))
                        if tlines:  # don't create a 0-byte .transcript.txt
                            transcript_file = str(
                                _write(slug, vid, tlines, out_dir, suffix=".transcript")
                            )
                        if rlines:  # don't create a 0-byte .related.txt
                            related_file = str(
                                _write(slug, vid, rlines, out_dir, suffix=".related")
                            )
                    add(
                        {
                            "url": url,
                            "video_id": vid,
                            "title": title,
                            "error": None,
                            "with_comments": with_comments,
                            "file": comment_file,
                            "comments": n_top,
                            "lines": n_lines,
                            "with_transcript": with_transcript,
                            "transcript_file": transcript_file,
                            "segments": n_seg,
                            "with_related": with_related,
                            "related_file": related_file,
                            "related": n_related,
                            "unified_file": unified_file,
                        }
                    )
                except Exception as exc:  # isolate this video; recycle the session
                    add({"url": url, "video_id": video_id, "error": str(exc) or type(exc).__name__})
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

    # --unify-all: join every per-video block (in input order) into one file and
    # stamp its path on each successful summary so main can report it.
    if unify_all and unified_blocks:
        ordered = [
            unified_blocks[v] for v in sorted(unified_blocks, key=lambda v: order.get(v, 1 << 30))
        ]
        doc = join_unified(ordered)
        if doc:
            path = Path(out_dir) / UNIFIED_ALL_FILENAME
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(doc) + "\n", encoding="utf-8")
            gp = str(path)
            for s in summaries:
                if not s.get("error"):
                    s["unified_file"] = gp
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
        prog="viewlyt",
        description="Scrape YouTube comments (likes, dates, replies) and optional transcript "
        "into out/<title-slug>-<video_id>.txt. Accepts many URLs and/or .txt/.csv files.",
        epilog=_EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {_pkg_version('viewlyt')}"
    )
    p.add_argument(
        "inputs",
        nargs="*",
        help="one or more video URLs/ids, and/or paths to .txt/.csv files listing them",
    )
    p.add_argument(
        "-f",
        "--from-file",
        action="append",
        default=[],
        metavar="PATH",
        help="file (.txt one-per-line, or .csv any column) with video URLs/ids; repeatable",
    )
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="number of concurrent browser instances (default: min(4, nº de vídeos))",
    )
    p.add_argument(
        "--limit-comments",
        "--limit",
        dest="limit",
        type=int,
        default=150,
        metavar="N",
        help="target top-level comments per video, or all if fewer (default: 150; "
        "--limit is a kept alias)",
    )
    p.add_argument(
        "--max-viewports",
        type=int,
        default=25,
        help="scroll budget per video (scroll-to-bottom steps, default: 25)",
    )
    p.add_argument(
        "--no-replies", action="store_true", help="don't expand/collect replies (faster)"
    )
    p.add_argument(
        "--limit-replies",
        "--max-replies",
        dest="max_replies",
        type=int,
        default=5,
        metavar="N",
        help="max replies per comment (default: 5; 0 disables; --max-replies is a kept alias)",
    )
    p.add_argument(
        "--no-merge-comments",
        "--prevent-comment-group",
        dest="merge_comments",
        action="store_false",
        default=True,
        help="don't merge consecutive top-level comments by the same author into one block "
        "(merging is ON by default; --prevent-comment-group is an accepted alias)",
    )
    p.add_argument(
        "-c",
        "--comments",
        action="store_true",
        help="collect comments (the default when no selector is given); combine with -t for both",
    )
    p.add_argument(
        "-t",
        "--transcript",
        action="store_true",
        help="collect the transcript -> out/<slug>-<id>.transcript.txt (skipped if the video has "
        "none, e.g. many music videos). Without -c this selects the transcript ONLY; add -c for "
        "both. NOTE: this changes the old meaning of --transcript, which also kept comments.",
    )
    p.add_argument(
        "--transcript-only",
        action="store_true",
        help="fetch only the transcript and skip comments (alias for -t without -c)",
    )
    p.add_argument(
        "-r",
        "--related",
        type=int,
        default=0,
        metavar="N",
        help="collect the first N related videos -> out/<slug>-<id>.related.txt "
        "(0 = off). Without -c this selects related ONLY; combine with -c/-t. "
        "Lists 'N. [<views> views. <title>](<url>)' — the sidebar exposes views, not likes.",
    )
    unify_group = p.add_mutually_exclusive_group()
    unify_group.add_argument(
        "--unify",
        action="store_true",
        help="write all of a video's products into ONE file out/<slug>-<id>.unified.txt "
        "(instead of separate .txt/.transcript.txt/.related.txt). Alone (no -c/-t/-r) it "
        f"collects everything (comments + transcript + {_UNIFY_DEFAULT_RELATED} related; "
        "override the count with -r N); with selectors it unifies only those.",
    )
    unify_group.add_argument(
        "--unify-all",
        action="store_true",
        help=f"like --unify but combine ALL videos into a single out/{UNIFIED_ALL_FILENAME} "
        "(no per-video files). Same collect-everything-when-alone rule as --unify.",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="visible browser instead of headless (more reliable vs the bot wall)",
    )
    p.add_argument(
        "--no-fallback",
        action="store_true",
        help="don't auto-retry headed when a block is detected",
    )
    p.add_argument(
        "--user-data-dir",
        default=None,
        help="persistent Chrome profile dir (a signed-in profile defeats the bot wall)",
    )
    p.add_argument(
        "-o",
        "--out-dir",
        default="out",
        help="directory for <title-slug>-<video_id>.txt (default: out)",
    )
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

    with_comments, with_transcript, with_related = resolve_modes(
        args.comments, args.transcript, args.transcript_only, args.related
    )
    related_limit = args.related
    # --unify/--unify-all alone (no product selector) collects EVERY product;
    # related needs a count, so default it (override with -r N).
    if (args.unify or args.unify_all) and not (
        args.comments or args.transcript or args.transcript_only or args.related > 0
    ):
        with_comments = with_transcript = with_related = True
        related_limit = _UNIFY_DEFAULT_RELATED

    jobs = args.jobs if args.jobs and args.jobs > 0 else min(4, len(targets))
    jobs = max(1, min(jobs, len(targets)))
    inner_progress = len(targets) == 1 and not args.quiet

    log.info("%d video(s) to scrape with %d browser instance(s)", len(targets), jobs)
    summaries = run_batch(
        targets,
        jobs=jobs,
        headless=not args.headed,
        fallback=not args.no_fallback,
        user_data_dir=args.user_data_dir,
        out_dir=args.out_dir,
        limit=args.limit,
        max_viewports=args.max_viewports,
        expand_replies=not args.no_replies,
        max_replies=args.max_replies,
        with_comments=with_comments,
        with_transcript=with_transcript,
        with_related=with_related,
        related_limit=related_limit,
        merge_comments=args.merge_comments,
        unify=args.unify,
        unify_all=args.unify_all,
        inner_progress=inner_progress,
        quiet=args.quiet,
    )

    ok = [s for s in summaries if not s["error"]]
    failed = [s for s in summaries if s["error"]]
    print(
        f"\nDone: {len(ok)}/{len(summaries)} video(s) scraped"
        + (f", {len(failed)} failed" if failed else "")
    )
    for s in ok:
        parts = []
        if args.unify or args.unify_all:
            counts = []
            if s.get("with_comments"):
                counts.append(f"{s['comments']} comments")
            if s.get("with_transcript"):
                counts.append(f"{s['segments']} segments")
            if s.get("with_related"):
                counts.append(f"{s['related']} related")
            parts.append(f"unified ({', '.join(counts) or 'empty'}) -> {s.get('unified_file')}")
        else:
            if s.get("with_comments"):
                parts.append(f"{s['comments']} comments, {s['lines']} lines -> {s['file']}")
            if s.get("with_transcript"):
                parts.append(
                    f"transcript: {s['segments']} segments -> {s['transcript_file']}"
                    if s.get("transcript_file")
                    else "transcript: unavailable"
                )
            if s.get("with_related"):
                parts.append(
                    f"related: {s['related']} videos -> {s['related_file']}"
                    if s.get("related_file")
                    else "related: unavailable"
                )
        print(f"  {_color('✓', '32')} {s['video_id']}  " + " | ".join(parts))
    for s in failed:
        print(f"  {_color('✗', '31')} {s['video_id']}  {s['error']}", file=sys.stderr)
    if failed and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
