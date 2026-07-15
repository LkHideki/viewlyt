"""Thin wrapper around ``yt-dlp`` for fast, Selenium-free metadata harvesting.

Used by two features: the channel-listing product (``vl -C``, primary engine —
see ``cli.run_channel_batch``) and the ``vl snap`` metadata snapshot. ``yt-dlp``
is a **base** dependency, but importing it costs about a second, so it is never
imported at module load time — only lazily, inside :func:`_extract`, the single
boundary every public function in this module funnels through. That keeps
``import viewlyt.ytdl`` (and therefore ``vl --help`` / ``vl snap --help``) cheap
and ``yt_dlp`` out of ``sys.modules`` until a function here actually runs.

Empirical notes from validating this module against a real channel
(``@veritasium``, 2026-07, yt-dlp 2026.7.4) — see plan-260715.md V1-V5/V9:

* **V1 (popular playlist) FAILED**: the auto-generated "Popular uploads"
  playlist (``PU{channel_id[2:]}``) is real but YouTube itself rejects it
  ("This playlist type is unviewable") — yt-dlp has no other way to request a
  popularity sort either. ``channel_entries(..., order="popular")`` therefore
  ALWAYS raises ``NotImplementedError`` here; the caller (``cli.py``) is
  expected to fall back straight to the Selenium chip-click path (Etapa 4) for
  ``popular``, unconditionally, not just on a lookup failure.
* **V2 confirmed**: the long-form uploads playlist ``UULF{channel_id[2:]}``
  resolves and contains only real videos (no Shorts/lives), newest-first
  (index 1 = newest, index N = oldest) — exactly like the ``/videos`` tab.
* **V3 confirmed, better than planned**: ``playlist_items: f"-{n}:"`` fetches
  only the last ``n`` entries in ONE request (~3.5s even on a 445-video
  channel) — yt-dlp does NOT enumerate the whole channel first. The returned
  slice is in ascending index order (least-old of the batch first, absolute
  oldest last), so it must be reversed in Python to present oldest-first.
* **V4 confirmed**: ``comment_count`` is populated by a plain ``extract_info``
  on a video URL — no ``getcomments: True`` (which is far more expensive)
  needed.
* **V5 confirmed**: ``channel_follower_count`` is populated on the tab (flat)
  extraction already used to resolve ``channel_id``.
* **V9**: floor pinned to ``yt-dlp>=2026.7.4`` (latest stable on PyPI as of
  this writing; a dated release close to today keeps the extractor current
  against YouTube's frequent layout churn).
"""

from __future__ import annotations

_BASE_OPTS: dict = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "socket_timeout": 20,
}


class _NullLogger:
    """Swallow yt-dlp's own stderr chatter; we surface failures via exceptions."""

    def debug(self, msg: str) -> None:  # pragma: no cover - trivial
        pass

    def warning(self, msg: str) -> None:  # pragma: no cover - trivial
        pass

    def error(self, msg: str) -> None:  # pragma: no cover - trivial
        pass


def _extract(url: str, opts: dict) -> dict:
    """Run ``yt_dlp.YoutubeDL(...).extract_info(url)`` and return the raw dict.

    The single I/O boundary of this module — ``yt_dlp`` is imported HERE, not at
    module scope, so ``import viewlyt.ytdl`` stays cheap and Selenium-free.
    Tests patch this function directly (``ytdl._extract``) rather than mocking
    yt-dlp's internals. Never catches exceptions: a bad/missing ref surfaces as
    ``yt_dlp.utils.DownloadError`` (or similar) straight to the caller, who
    decides whether to fall back to Selenium.
    """
    import yt_dlp

    full_opts = {**_BASE_OPTS, "logger": _NullLogger(), **opts}
    with yt_dlp.YoutubeDL(full_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info or {}


def _entry_to_item(e: dict) -> dict:
    """Flat playlist/tab entry -> ``{"video_id", "title", "views", "url"}``.

    Pure (no I/O): the shape matches what ``htmltext.format_related`` expects,
    so channel listings can reuse it verbatim. ``views`` is pre-formatted with
    :func:`viewlyt.htmltext.format_view_count` (``""`` when ``view_count`` is
    missing/zero, e.g. most ``old``-order entries where YouTube's flat tab
    payload tends to omit it).
    """
    from .htmltext import format_view_count

    video_id = e.get("id") or ""
    url = e.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    return {
        "video_id": video_id,
        "title": e.get("title") or "",
        "views": format_view_count(e.get("view_count")),
        "url": url,
    }


def _channel_id_for(ref: str) -> tuple[str, str]:
    """Resolve ``ref`` (e.g. ``"@veritasium"``) to ``(channel_name, channel_id)``.

    One cheap request (``playlistend: 1``) against the ``/videos`` tab — flat
    extraction, so it's fast (~0.5s) regardless of channel size.
    """
    info = _extract(
        f"https://www.youtube.com/{ref}/videos",
        {"extract_flat": "in_playlist", "playlistend": 1},
    )
    return (info.get("channel") or info.get("uploader") or "", info.get("channel_id") or "")


def channel_entries(ref: str, n: int, order: str = "recent") -> tuple[str, list[dict]]:
    """List up to ``n`` videos for channel ``ref``, sorted by ``order``.

    Returns ``(channel_name, items)`` where each item is the
    :func:`_entry_to_item` shape. Does **not** catch exceptions — a raised
    ``yt_dlp.utils.DownloadError`` (bad ref, network failure, ...) or the
    ``NotImplementedError`` below (unsupported ``order``) is the caller's
    signal to fall back to Selenium (see ``cli.run_channel_batch``).

    * ``"recent"`` — the channel's ``/videos`` tab, newest first. One flat
      request, ``playlistend: n``.
    * ``"popular"`` — **always raises** ``NotImplementedError``: YouTube
      rejects the ``PU...`` "Popular uploads" auto-playlist yt-dlp would
      otherwise use (confirmed empirically; see the module docstring). The
      caller must route ``popular`` straight to the Selenium fallback.
    * ``"old"`` — the long-form uploads playlist (``UULF...``), oldest first.
      One flat request with ``playlist_items: f"-{n}:"`` (fetches only the
      tail, not the whole channel), then reversed in Python since the tail
      slice comes back least-old-of-the-batch-first.
    """
    if order == "recent":
        info = _extract(
            f"https://www.youtube.com/{ref}/videos",
            {"extract_flat": "in_playlist", "playlistend": max(n, 1)},
        )
        name = info.get("channel") or info.get("uploader") or ""
        entries = (info.get("entries") or [])[:n]
        return name, [_entry_to_item(e) for e in entries]

    if order == "popular":
        raise NotImplementedError(
            "yt-dlp has no supported way to list a channel's popular uploads "
            "(YouTube rejects the PU... auto-playlist) — use the Selenium fallback"
        )

    if order == "old":
        name, channel_id = _channel_id_for(ref)
        if not channel_id:
            return name, []
        playlist_id = "UULF" + channel_id[2:]
        playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        info = _extract(
            playlist_url,
            {"extract_flat": "in_playlist", "playlist_items": f"-{max(n, 1)}:"},
        )
        entries = list(reversed(info.get("entries") or []))[:n]
        return name, [_entry_to_item(e) for e in entries]

    raise ValueError(f"unknown order: {order!r} (expected recent|popular|old)")


def video_info(video_id: str) -> dict:
    """Full (non-flat) metadata for one video, for ``vl snap``.

    Returns yt-dlp's raw info dict (title, channel, channel_url, upload_date,
    duration, view_count, like_count, comment_count, description, chapters,
    tags, ...) — ``snap.format_video_snapshot`` picks the fields it needs and
    tolerates missing/``None`` ones (e.g. ``chapters`` is ``None`` on videos
    without chapter markers; ``comment_count``/``like_count`` can be ``None``
    when disabled by the uploader).
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    return _extract(url, {})


def channel_info(ref: str, latest: int = 5) -> dict:
    """Channel metadata + its ``latest`` most recent videos, for ``vl snap``.

    One flat request against the ``/videos`` tab (``playlistend: latest``).
    Returns the raw tab info dict — carries ``channel``/``uploader``,
    ``channel_id``, ``channel_url``, ``channel_follower_count``,
    ``description``, and ``entries`` (flat; feed through :func:`_entry_to_item`
    for the "Latest videos" section).
    """
    info = _extract(
        f"https://www.youtube.com/{ref}/videos",
        {"extract_flat": "in_playlist", "playlistend": max(latest, 1)},
    )
    return info
