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

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date

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
    """Flat playlist/tab entry -> ``{"video_id", "title", "views", "views_int", "url"}``.

    Pure (no I/O): the shape matches what ``htmltext.format_related`` expects,
    so channel listings can reuse it verbatim.

    ``views_int`` is yt-dlp's EXACT ``view_count`` and is the source of truth
    (``None`` when the payload omits it, e.g. most ``old``-order entries).
    ``views`` is the rendered form kept only so the dict stays drop-in
    compatible with the Selenium fallback's shape; ``format_related`` prefers
    the int. Library callers wanting a number must read ``views_int`` — never
    re-parse the string.
    """
    from .htmltext import format_view_count

    video_id = e.get("id") or ""
    url = e.get("url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    raw = e.get("view_count")
    views_int = int(raw) if isinstance(raw, (int, float)) and raw > 0 else None
    return {
        "video_id": video_id,
        "title": e.get("title") or "",
        "views": format_view_count(views_int),
        "views_int": views_int,
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


# ---------------------------------------------------------------------------
# Channel window (--since/--until): every video published inside a date range.
#
# Cost model that shapes everything below (measured against @veritasium): the
# flat ``/videos`` tab carries NO publish date, and a date costs one full
# extraction per video (~1.3 s), while a flat scan is cheap (64 entries ~1.1 s,
# 256 ~3.4 s) and has no cursor, so growing it just re-reads the prefix. The
# tab is newest-first, so the window is a contiguous slice — find its two
# borders by binary search and pay O(log N) dated probes instead of N.
# ---------------------------------------------------------------------------


class _WindowScan:
    """State of one :func:`scan_window` run; see there for the contract.

    Probes are memoised BY INDEX in ``dates`` (``None`` = undatable). Galloping
    re-fetches a longer prefix of the same list, so an index keeps meaning the
    same entry across fetches and no entry is ever dated twice.

    ``amb`` is the inclusive hull of indices whose position can't be trusted:
    the newest-first order is an assumption (premieres/VODs can break it), so
    every new probe is checked against its nearest trusted neighbours, and a
    contradiction grows the hull over the pair. Hull entries are dated one by
    one and judged by their own date; the bisection only ever sees the entries
    OUTSIDE it, which stay mutually consistent by construction.
    """

    def __init__(
        self,
        fetch_flat: Callable[[int], list[dict]],
        probe_date: Callable[[dict], date | None],
        since: date,
        until: date,
        cap: int,
        first_batch: int,
    ) -> None:
        self.fetch_flat = fetch_flat
        self.probe_date = probe_date
        self.since = since
        self.until = until
        self.cap = cap
        self.first_batch = first_batch
        self.entries: list[dict] = []
        self.dates: dict[int, date | None] = {}
        self.probes = 0
        self.flat_requests = 0
        self.truncated = False
        self.amb: tuple[int, int] | None = None
        self.amb_grew = False

    # -- bookkeeping ---------------------------------------------------------

    def in_amb(self, i: int) -> bool:
        return self.amb is not None and self.amb[0] <= i <= self.amb[1]

    def fetch(self, n: int) -> None:
        # Clip defensively: `n` is the contract (`playlistend`), and a longer
        # reply would break the "short reply == exhausted" test in `gallop`.
        self.entries = list(self.fetch_flat(n))[:n]
        self.flat_requests += 1

    def trusted(self) -> list[tuple[int, date]]:
        """Datable probes outside the ambiguous hull, as ``(index, date)``."""
        return [(i, d) for i, d in self.dates.items() if d is not None and not self.in_amb(i)]

    def date_at(self, i: int) -> date | None:
        """Publish date of ``entries[i]`` (memoised), guarding the newest-first order."""
        if i in self.dates:
            return self.dates[i]
        d = self.probe_date(self.entries[i])
        self.probes += 1
        self.dates[i] = d
        if d is not None and not self.in_amb(i):
            self._check_order(i, d)
        return d

    def _check_order(self, i: int, d: date) -> None:
        # The trusted set is monotone (invariant), so a new probe that fits
        # between its two nearest trusted neighbours fits everywhere.
        pred: tuple[int, date] | None = None
        succ: tuple[int, date] | None = None
        for j, dj in self.trusted():
            if j < i and (pred is None or j > pred[0]):
                pred = (j, dj)
            elif j > i and (succ is None or j < succ[0]):
                succ = (j, dj)
        if pred is not None and pred[1] < d:  # an EARLIER index dated OLDER: contradiction
            self._grow_amb(pred[0], i)
        if succ is not None and d < succ[1]:
            self._grow_amb(i, succ[0])

    def _grow_amb(self, a: int, b: int) -> None:
        lo, hi = (a, b) if self.amb is None else (min(a, self.amb[0]), max(b, self.amb[1]))
        self.amb = (lo, hi)
        self.amb_grew = True

    def date_range(self, a: int, b: int) -> None:
        """Date every entry in ``[a, b]`` — the linear degrade for the hull."""
        for i in range(a, b + 1):
            self.date_at(i)

    def nearest(self, mid: int, a: int, b: int) -> tuple[int, date] | None:
        """Nearest trusted datable index to ``mid`` inside ``[a, b)``, or ``None``.

        Deterministic outward order (mid, mid+1, mid-1, mid+2, ...). Hull indices
        are skipped without a probe: they are judged by their own date later.
        """
        for off in range(b - a):
            for j in (mid,) if off == 0 else (mid + off, mid - off):
                if a <= j < b and not self.in_amb(j):
                    d = self.date_at(j)
                    if self.amb_grew:  # the probe exposed an inversion: `run` redoes the search
                        return None
                    if d is not None:
                        return j, d
        return None

    # -- phases --------------------------------------------------------------

    def oldest_trusted(self) -> date | None:
        """Date of the oldest datable, trusted entry fetched so far (``None`` if none)."""
        i = len(self.entries) - 1
        while i >= 0:
            if self.amb is not None and self.in_amb(i):
                i = self.amb[0] - 1
                continue
            d = self.date_at(i)
            if d is not None and not self.in_amb(i):
                return d
            i -= 1
        return None

    def gallop(self) -> None:
        """Grow the flat scan (x2) until its oldest entry predates ``since``.

        A reply shorter than asked means the channel is exhausted. Stops at
        ``cap`` with ``truncated`` set when the window was still open.
        """
        requested = self.first_batch if not self.cap else min(self.first_batch, self.cap)
        self.fetch(requested)
        while len(self.entries) >= requested:
            oldest = self.oldest_trusted()
            if oldest is not None and oldest < self.since:
                return
            if self.cap and requested >= self.cap:
                self.truncated = True
                return
            requested = requested * 2 if not self.cap else min(requested * 2, self.cap)
            self.fetch(requested)

    def first(self, pred: Callable[[date], bool], *, blind_at_end: bool, edge: bool = False) -> int:
        """Smallest index whose trusted date satisfies ``pred`` (``len`` if none).

        ``pred`` must be false-then-true along the newest-first order. The search
        starts from the bracket every earlier probe already implies, so gallop
        probes are reused for free. ``edge`` tests the left border first: with
        ``until`` at or after the newest video (the usual "up to now") that one
        probe replaces a whole bisection.

        Undatable entries never crash the search: the pivot slides to the nearest
        datable index. A region with NO datable entry is resolved conservatively,
        INSIDE the window — keeping an entry whose date is unknown beats silently
        dropping one that may belong. ``blind_at_end`` says which border is being
        searched: for the older one (``True``) the answer is the region's far edge,
        for the newer one (``False``) its near edge, so either way the region
        lands between the two borders.
        """
        a, b = 0, len(self.entries)
        for i, d in self.trusted():
            if pred(d):
                b = min(b, i)
            else:
                a = max(a, i + 1)
        probe_edge = edge
        while a < b and not self.amb_grew:
            hit = self.nearest(a if probe_edge else (a + b) // 2, a, b)
            probe_edge = False
            if hit is None:
                return b if blind_at_end else a
            j, d = hit
            if pred(d):
                b = j
            else:
                a = j + 1
        return a

    def bounds(self) -> tuple[int, int]:
        """``(hi, lo)``: the window is ``entries[hi:lo]`` (newest first)."""
        lo = self.first(lambda d: d < self.since, blind_at_end=True)
        if lo == 0:  # even the newest entry is older than `since`: no need to find hi
            return 0, 0
        hi = self.first(lambda d: d <= self.until, blind_at_end=False, edge=True)
        return min(hi, lo), lo

    def run(self) -> tuple[int, int]:
        self.gallop()
        while True:
            if self.amb is not None:
                self.date_range(*self.amb)
            self.amb_grew = False
            hi, lo = self.bounds()
            if not self.amb_grew:  # else the hull grew mid-search: redo it without the hull
                return hi, lo

    def window(self, hi: int, lo: int) -> list[dict]:
        """Entries inside the window, newest first, each carrying its known ``_date``."""
        out: list[dict] = []
        for k, e in enumerate(self.entries):
            d = self.dates.get(k)
            if d is not None and self.in_amb(k):
                inside = self.since <= d <= self.until  # hull: the actual date decides
            else:
                inside = hi <= k < lo
            if inside:
                if d is not None:
                    e["_date"] = d
                out.append(e)
        return out


def scan_window(
    fetch_flat: Callable[[int], list[dict]],
    probe_date: Callable[[dict], date | None],
    since: date,
    until: date,
    *,
    cap: int = 5000,
    first_batch: int = 64,
) -> tuple[list[dict], dict]:
    """Entries of a newest-first channel list published in ``[since, until]``.

    PURE: all I/O is injected (the same idea as ``tokens.split_by_budget`` taking
    its counter), so the search is testable with plain lists of dates.

    * ``fetch_flat(n)`` -> the first ``n`` flat entries, newest first; fewer than
      ``n`` means the channel is exhausted.
    * ``probe_date(entry)`` -> the exact publish date, or ``None`` when it can't
      be had (private/removed). The expensive call — this function exists to
      make as few of them as possible.
    * ``since``/``until`` are INCLUSIVE: a video published exactly on either
      day is in. ``since > until`` is an empty window and does no I/O.
    * ``cap`` bounds the flat entries ever requested (``0`` = unlimited).

    Method: **gallop** — fetch ``first_batch``, date its LAST entry, and double
    the request while that entry is still ``>= since`` (the channel's size is
    unknown up front and the tab has no cursor); then **bisect** the in-memory
    list for the window's two borders. A monotonic channel costs O(log N)
    probes (~28 for 3000 videos), never one per video.

    Newest-first order is an assumption, not a guarantee (premieres and VODs
    can be listed out of place). Probes that contradict it are detected; the
    span between the contradicting pair is then dated entry by entry and
    judged by actual date, ``meta["non_monotonic"]`` is set, and the result is
    correct for everything dated. LIMIT: only inversions between PROBED entries
    are visible — an out-of-place entry that no other probe contradicts can
    shift a border or slip through unnoticed, so callers that date the returned
    items anyway should re-filter them by date.

    Undatable entries are routed around (nearest datable neighbour); a region
    with no datable entry at all is kept INSIDE the window, never dropped.

    Every returned entry whose date was learned carries it as ``entry["_date"]``
    (a ``date``) — already paid for, so callers needn't fetch it again.

    ``meta``: ``probes`` (``probe_date`` calls), ``flat_requests``, ``scanned``
    (flat entries in the final fetch), ``truncated_by_cap`` (the cap stopped the
    gallop while the window was still open — the result may be incomplete) and
    ``non_monotonic``.
    """
    if first_batch < 1:
        raise ValueError(f"first_batch must be >= 1, got {first_batch}")
    if cap < 0:
        raise ValueError(f"cap must be >= 0 (0 = unlimited), got {cap}")
    meta = {
        "probes": 0,
        "flat_requests": 0,
        "scanned": 0,
        "truncated_by_cap": False,
        "non_monotonic": False,
    }
    if since > until:
        return [], meta
    scan = _WindowScan(fetch_flat, probe_date, since, until, cap, first_batch)
    hi, lo = scan.run()
    items = scan.window(hi, lo)
    meta.update(
        probes=scan.probes,
        flat_requests=scan.flat_requests,
        scanned=len(scan.entries),
        truncated_by_cap=scan.truncated,
        non_monotonic=scan.amb is not None,
    )
    return items, meta


def video_upload_date(video_id: str) -> date | None:
    """Exact publish date of one video (UTC), via a full extraction (~1.3 s).

    yt-dlp reports ``upload_date`` as ``"yyyymmdd"``; ``None`` when it is
    missing (premieres/upcoming) or unparseable. Does not catch exceptions —
    a private/removed/network failure surfaces to the caller, who knows
    whether one bad video should sink the batch (see :func:`fill_dates`).
    """
    info = _extract(f"https://www.youtube.com/watch?v={video_id}", {})
    raw = str(info.get("upload_date") or "")
    if len(raw) != 8 or not (raw.isascii() and raw.isdigit()):
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def channel_entries_window(
    ref: str,
    since: date,
    until: date,
    *,
    order: str = "recent",
    cap: int = 5000,
) -> tuple[str, list[dict], dict]:
    """Every video of channel ``ref`` published in ``[since, until]`` (inclusive).

    Returns ``(channel_name, items, meta)``: ``items`` use the
    :func:`_entry_to_item` shape, plus ``"date"`` (``"yyyy-mm-dd"``) on the ones
    whose date the search already learned — the rest are undated here (see
    :func:`fill_dates`); ``meta`` is :func:`scan_window`'s.

    * ``"recent"`` — newest first (the ``/videos`` tab order).
    * ``"old"`` — the same window, reversed in Python.
    * ``"popular"`` — ``NotImplementedError``: a date window can't be combined
      with a popularity sort (yt-dlp can't list popular uploads at all, see the
      module docstring); the caller routes it elsewhere.
    * anything else — ``ValueError``. Both are raised before any network call.

    Same error contract as :func:`channel_entries`: exceptions are NOT caught
    — a raised error is the caller's cue to fall back to Selenium. A returned
    empty ``items`` is ambiguous on its own: ``meta["scanned"] == 0`` means the
    tab itself came back empty (fall back), while ``scanned > 0`` means the
    window genuinely holds no videos. A single failing date probe also
    propagates rather than being read as "undatable": swallowing a blanket
    network failure would make every entry look undatable and silently return
    the whole scan.
    """
    if order == "popular":
        raise NotImplementedError(
            "a date window can't be combined with order='popular' "
            "(yt-dlp can't list popular uploads) — use the Selenium fallback"
        )
    if order not in ("recent", "old"):
        raise ValueError(f"unknown order: {order!r} (expected recent|old)")

    name = ""

    def fetch_flat(n: int) -> list[dict]:
        nonlocal name
        info = _extract(
            f"https://www.youtube.com/{ref}/videos",
            {"extract_flat": "in_playlist", "playlistend": n},
        )
        name = name or info.get("channel") or info.get("uploader") or ""
        return list(info.get("entries") or [])

    def probe(entry: dict) -> date | None:
        vid = entry.get("id") or ""
        return video_upload_date(vid) if vid else None

    entries, meta = scan_window(fetch_flat, probe, since, until, cap=cap)
    items = []
    for e in entries:
        item = _entry_to_item(e)
        if isinstance(e.get("_date"), date):
            item["date"] = e["_date"].isoformat()
        items.append(item)
    if order == "old":
        items.reverse()
    return name, items, meta


def fill_dates(items: list[dict], *, workers: int = 4) -> int:
    """Date every item lacking a non-empty ``"date"``; returns how many got one.

    One full extraction per item (~1.3 s), so they run on a small thread pool:
    the work is network-bound, and ``workers`` stays low (4) as a ceiling
    against YouTube rate-limiting rather than for CPU. One failing video
    (private, removed, transient error) is left undated instead of failing the
    batch. Items without a ``video_id`` are skipped. ``items`` keeps its order;
    ``"date"`` is written from the calling thread, never from a worker.
    """
    todo = [it for it in items if not it.get("date") and it.get("video_id")]
    if not todo:
        return 0

    def one(item: dict) -> date | None:
        try:
            return video_upload_date(item["video_id"])
        except Exception:  # one bad video must not sink the rest
            return None

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
        dates = list(pool.map(one, todo))
    filled = 0
    for item, d in zip(todo, dates, strict=True):
        if d is not None:
            item["date"] = d.isoformat()
            filled += 1
    return filled


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


# --------------------------------------------------------------------------- #
# Transcript from YouTube's own caption tracks (the default `vl` product)
# --------------------------------------------------------------------------- #
def _json3_to_segments(data: dict) -> list[tuple[str, str]]:
    """YouTube ``json3`` caption payload -> ``[(timestamp, text), ...]``.

    Pure (no I/O). The shape is exactly what ``scraper.fetch_transcript`` returns
    and ``htmltext.format_transcript`` consumes, so the yt-dlp transcript is a
    drop-in for the Selenium panel scrape. The timestamp is ``m:ss`` /
    ``h:mm:ss`` (``format_duration`` of the event start), the text has its
    whitespace collapsed (manual tracks embed ``\\n`` mid-caption).

    Skipped events: ``aAppend`` ones (ASR tracks interleave bare ``"\\n"``
    line-break events that are layout, not speech), events without ``segs``
    (window/style housekeeping) and events whose text is empty once collapsed.
    Never raises: any non-dict/garbage input yields ``[]``.
    """
    from .htmltext import format_duration

    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    segments: list[tuple[str, str]] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("aAppend") or not isinstance(ev.get("segs"), list):
            continue
        raw = "".join(s.get("utf8") or "" for s in ev["segs"] if isinstance(s, dict))
        text = " ".join(raw.split())
        if not text:
            continue
        start = ev.get("tStartMs")
        ts = format_duration(int(start) // 1000) if isinstance(start, (int, float)) else ""
        segments.append((ts, text))
    return segments


def _json3_url(tracks: object) -> str | None:
    """The ``json3`` URL inside one yt-dlp track list (``[{"ext", "url"}, ...]``)."""
    if not isinstance(tracks, list):
        return None
    for fmt in tracks:
        if isinstance(fmt, dict) and fmt.get("ext") == "json3" and fmt.get("url"):
            return fmt["url"]
    return None


def _pick_caption_track(info: dict) -> str | None:
    """``json3`` URL of the track the YouTube transcript panel shows by default.

    Pure. The panel prefers a human-made track in the video's own language, then
    the ASR (auto-generated) track of that same language. Priority:

    1. manual ``subtitles[language]``;
    2. ASR ``automatic_captions["<language>-orig"]`` (yt-dlp keys the ORIGINAL
       ASR track with ``-orig``; the plain key can be a re-translation);
    3. ASR ``automatic_captions[language]``;
    4. the first ASR key ending in ``-orig`` (``language`` missing or off);
    5. the first manual track that isn't ``live_chat`` (a replay's chat log is
       filed under ``subtitles`` but is not a caption).

    A track with no ``json3`` entry is skipped (next priority). ``None`` when
    nothing qualifies — in particular a translated ASR track is NEVER a last
    resort: ASR translations (~150 of them per video) would be machine output
    of machine output, worse than the Selenium panel fallback.
    """
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    language = info.get("language")

    candidates: list[object] = []
    if language:
        candidates += [subs.get(language), auto.get(f"{language}-orig"), auto.get(language)]
    candidates.append(next((v for k, v in auto.items() if k.endswith("-orig")), None))
    candidates.append(next((v for k, v in subs.items() if k != "live_chat"), None))
    for tracks in candidates:
        url = _json3_url(tracks)
        if url:
            return url
    return None


def _extract_with_captions(video_id: str) -> tuple[dict, dict | None]:
    """``(info, json3_payload_or_None)`` for one video, in a single yt-dlp session.

    The module's second I/O boundary (next to :func:`_extract`): ``yt_dlp`` is
    imported HERE, lazily. One ``YoutubeDL`` does both ``extract_info`` and the
    caption download (``ydl.urlopen`` reuses its cookies/headers/proxy, ~0.2 s
    for the json3 file). The payload is ``None`` when the video has no usable
    caption track. Never catches exceptions. Tests patch THIS function, never
    yt-dlp internals.
    """
    import json

    import yt_dlp

    opts = {**_BASE_OPTS, "logger": _NullLogger()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        info = info or {}
        url = _pick_caption_track(info)
        payload = json.loads(ydl.urlopen(url).read()) if url else None
    return info, payload


def video_transcript(video_id: str) -> dict:
    """Transcript + title + exact views of one video, WITHOUT a browser.

    Returns ``{"video_id", "title", "views_int", "segments"}``: ``segments`` is the
    :func:`_json3_to_segments` shape (``[]`` when the video has no caption
    track), ``views_int`` the exact ``view_count`` (``None`` when absent or not
    positive — same normalisation as :func:`_entry_to_item`).

    Does **not** catch exceptions — a raised ``yt_dlp.utils.DownloadError`` /
    network error or an empty ``segments`` is the caller's signal to fall back
    to the Selenium transcript panel (see ``cli.run_batch``), the same contract
    as :func:`channel_entries`.
    """
    info, payload = _extract_with_captions(video_id)
    raw = info.get("view_count")
    views_int = int(raw) if isinstance(raw, (int, float)) and raw > 0 else None
    return {
        "video_id": video_id,
        "title": info.get("title") or "",
        "views_int": views_int,
        "segments": _json3_to_segments(payload) if payload else [],
    }
