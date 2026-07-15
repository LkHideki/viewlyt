"""``vl snap`` — a quick metadata snapshot of a video or channel, via yt-dlp.

Selenium-free (like ``vl ask``/``vl split``/``vl watch``): the only heavy dep
is ``yt_dlp``, reached exclusively through :mod:`viewlyt.ytdl`, which keeps it
out of ``sys.modules`` until a snapshot is actually fetched — ``vl snap --help``
stays cheap.

Unlike the main scrape (comments/transcript/related), a snapshot is a single
small request: title, channel, views, likes, comment count, publish date,
duration, description, chapters and tags for a video; name, handle, follower
count, description and the latest few uploads for a channel.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version as _pkg_version
from pathlib import Path

from . import ytdl
from .clipboard import copy_to_clipboard
from .htmltext import (
    extract_channel_ref,
    extract_video_id,
    flatten_inline,
    format_duration,
    format_related,
    slugify,
)


def detect_target(text: str) -> tuple[str, str]:
    """Classify ``text`` as a channel or a video reference.

    Tries :func:`viewlyt.htmltext.extract_channel_ref` FIRST, then
    :func:`viewlyt.htmltext.extract_video_id` — a channel reference (bare
    ``@handle``, ``/channel/UC...``, ``/c/<name>``, ``/user/<name>``) is
    checked before falling back to the video-id extraction, whose last-resort
    "any 11-char run" heuristic would otherwise misclassify some channel
    references. Returns ``("channel", ref)`` or ``("video", video_id)``.
    Raises ``ValueError`` when neither parser accepts ``text``.
    """
    try:
        return "channel", extract_channel_ref(text)
    except ValueError:
        pass
    return "video", extract_video_id(text)


def format_video_snapshot(info: dict) -> list[str]:
    """Render a yt-dlp video info dict as Markdown snapshot lines.

    Pure (no I/O). Tolerates missing/``None`` fields — absent metrics/sections
    are simply omitted, never rendered as ``"None"`` or an empty heading.
    """
    title = flatten_inline(info.get("title") or "")
    out: list[str] = [f"# {title}"] if title else []
    out.append("")

    channel = info.get("channel") or info.get("uploader") or ""
    channel_url = info.get("channel_url") or info.get("uploader_url") or ""
    if channel:
        out.append(
            f"- Channel: {channel} ({channel_url})" if channel_url else f"- Channel: {channel}"
        )

    upload_date = info.get("upload_date") or ""
    if upload_date and len(upload_date) == 8:
        out.append(f"- Published: {upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}")

    duration = format_duration(info.get("duration"))
    if duration:
        out.append(f"- Duration: {duration}")

    view_count = info.get("view_count")
    if view_count:
        out.append(f"- Views: {view_count:,}")

    like_count = info.get("like_count")
    if like_count:
        out.append(f"- Likes: {like_count:,}")

    comment_count = info.get("comment_count")
    if comment_count:
        out.append(f"- Comments: {comment_count:,}")

    description = (info.get("description") or "").strip()
    if description:
        out.append("")
        out.append("## Description")
        out.append(description)

    chapters = info.get("chapters") or []
    if chapters:
        out.append("")
        out.append("## Chapters")
        for i, ch in enumerate(chapters, 1):
            ts = format_duration(ch.get("start_time"))
            ch_title = flatten_inline(ch.get("title") or "")
            out.append(f"{i}. [{ts}] {ch_title}" if ts else f"{i}. {ch_title}")

    tags = info.get("tags") or []
    if tags:
        out.append("")
        out.append("## Tags")
        out.append(", ".join(tags))

    while out and out[-1] == "":
        out.pop()
    return out


def format_channel_snapshot(info: dict) -> list[str]:
    """Render a yt-dlp channel (tab) info dict as Markdown snapshot lines.

    Pure (no I/O). Reuses :func:`viewlyt.htmltext.format_related` for the
    "Latest videos" section — the flat entries share the same
    ``{"title","views","url"}`` shape once run through
    :func:`viewlyt.ytdl._entry_to_item`.
    """
    from .ytdl import _entry_to_item

    name = flatten_inline(info.get("channel") or info.get("uploader") or "")
    out: list[str] = [f"# {name}"] if name else []
    out.append("")

    handle = info.get("uploader_id") or info.get("channel_id") or ""
    channel_url = info.get("channel_url") or info.get("uploader_url") or ""
    if handle:
        out.append(f"- Handle: {handle} ({channel_url})" if channel_url else f"- Handle: {handle}")

    followers = info.get("channel_follower_count")
    if followers:
        out.append(f"- Subscribers: {followers:,}")

    channel_id = info.get("channel_id") or ""
    if channel_id:
        out.append(f"- Channel id: {channel_id}")

    description = (info.get("description") or "").strip()
    if description:
        out.append("")
        out.append("## Description")
        out.append(description)

    entries = info.get("entries") or []
    if entries:
        out.append("")
        out.append("## Latest videos")
        items = [_entry_to_item(e) for e in entries]
        out.extend(format_related(items))

    while out and out[-1] == "":
        out.pop()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vl snap",
        description="Quick metadata snapshot (title, channel, views, likes, comments, "
        "date, duration, description, chapters, tags) of a video or channel, via yt-dlp. "
        "Selenium-free and fast — not a replacement for the full scrape.",
    )
    p.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {_pkg_version('viewlyt')}"
    )
    p.add_argument("target", help="a video/channel URL, a bare @handle, or a video id")
    p.add_argument(
        "-o", "--out-dir", default="out", help="directory for the .snapshot.md (default: out)"
    )
    p.add_argument(
        "--copy", action="store_true", help="also copy the snapshot to the system clipboard"
    )
    p.add_argument("-q", "--quiet", action="store_true", help="suppress the stdout preview")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        kind, ref = detect_target(args.target)
    except ValueError as exc:
        print(f"vl snap: {exc}", file=sys.stderr)
        return 2

    try:
        if kind == "video":
            info = ytdl.video_info(ref)
            lines = format_video_snapshot(info)
            slug = slugify(info.get("title") or "")
            filename = f"{slug}-{ref}.snapshot.md"
        else:
            info = ytdl.channel_info(ref)
            lines = format_channel_snapshot(info)
            slug = slugify(info.get("channel") or info.get("uploader") or ref.lstrip("@"))
            filename = f"{slug}.snapshot.md"
    except Exception as exc:  # yt_dlp.utils.DownloadError or similar network/lookup failure
        print(f"vl snap: failed to fetch {args.target!r}: {exc}", file=sys.stderr)
        return 1

    text = "\n".join(lines) + ("\n" if lines else "")

    if not args.quiet:
        print(text)

    out_path = Path(args.out_dir) / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    if not args.quiet:
        print(f"-> {out_path}", file=sys.stderr)

    if args.copy:
        if copy_to_clipboard(text):
            if not args.quiet:
                print("(copied to clipboard)", file=sys.stderr)
        else:
            print(
                "vl snap: couldn't find a clipboard tool (pbcopy/clip/xclip/xsel)", file=sys.stderr
            )

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
