"""Thin wrapper around ``yt-dlp`` for fast, Selenium-free metadata harvesting.

Used by four features: the default transcript product (captions without a
browser — :func:`video_transcript`), the channel-listing product (``vl -C``,
primary engine — see ``cli.run_channel_batch``), its date-window mode
(``--since``/``--until`` — :func:`channel_entries_window`, :func:`fill_dates`)
and the ``vl snap`` metadata snapshot. ``yt-dlp``
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

import logging
import math
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date

log = logging.getLogger("viewlyt")

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

    ``views_int`` is yt-dlp's ``view_count`` and is the source of truth for the
    number (``None`` when the payload omits it, e.g. most ``old``-order
    entries) — but on a channel TAB it is the tab's own ROUNDED count (live
    check: 17,000,000 where the watch page says 17,320,592), exact only from a
    watch-page extraction (``video_transcript``, ``video_info``). ``views`` is
    the rendered form kept only so the dict stays drop-in compatible with the
    Selenium fallback's shape; ``format_related`` prefers the int. Library
    callers wanting a number must read ``views_int`` — never re-parse the string.
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


class WindowScanError(RuntimeError):
    """The date search gave up instead of returning a window it can't vouch for.

    Raised by :func:`scan_window` when the probe budget is spent or when videos
    stop being datable altogether. ``cli.run_channel_batch`` already treats any
    exception from the yt-dlp window listing as "use the other engine".
    """


def _out_of_order(seq: list[date]) -> set[int]:
    """Positions of ``seq`` that are NOT in every longest non-increasing subsequence.

    Pure. A newest-first list of dates is non-increasing; when two probes
    contradict each other, the entries whose removal restores the order are the
    suspects. A lone out-of-place entry comes out alone (its neighbours vouch for
    the rest); when the evidence ties — either of two adjacent entries could be
    the odd one — BOTH are returned, because guessing wrong would trust the
    outlier. Empty for an already ordered sequence. O(n²): ``seq`` holds probes,
    a few dozen at most.
    """
    n = len(seq)
    ending = [1] * n  # longest non-increasing run ending at i
    for i in range(n):
        for j in range(i):
            if seq[j] >= seq[i] and ending[j] + 1 > ending[i]:
                ending[i] = ending[j] + 1
    starting = [1] * n  # ... starting at i
    for i in range(n - 1, -1, -1):
        for j in range(i + 1, n):
            if seq[i] >= seq[j] and starting[j] + 1 > starting[i]:
                starting[i] = starting[j] + 1
    best = max(ending, default=0)
    on_a_best_run = [i for i in range(n) if ending[i] + starting[i] - 1 == best]
    per_level: dict[int, int] = {}
    for i in on_a_best_run:
        per_level[ending[i]] = per_level.get(ending[i], 0) + 1
    # A run visits each level once, so an entry alone on its level is on EVERY best run.
    certain = {i for i in on_a_best_run if per_level[ending[i]] == 1}
    return set(range(n)) - certain


class _WindowScan:
    """State of one :func:`scan_window` run; see there for the contract.

    Probes are memoised BY VIDEO ID in ``known`` (``None`` = undatable), never by
    list position: the channel keeps publishing while the scan gallops, so a
    re-fetched list can have a new video at the front or a removed one in the
    middle, and every index shifts. An id keeps meaning the same video, so no
    video is dated twice and no entry can be handed another one's date. An entry
    without an id has nothing to look up and stays undatable.

    The newest-first order is an assumption (premieres/VODs can break it), so
    every fresh probe is checked against its nearest trusted neighbours. A
    contradiction is resolved locally: the neighbours of the probe and of the
    entry it contradicts are dated too, and the smallest set of entries whose
    removal restores the order goes into ``bad`` — judged by their own date from
    then on, while the bisection sees only the rest. ``bad`` holds ids, so each
    out-of-place entry (or run of them) is its own island: two distant
    inversions never fuse into one huge span, and a re-fetch can't misplace them.
    """

    def __init__(
        self,
        fetch_flat: Callable[[int], list[dict]],
        probe_date: Callable[[dict], date | None],
        since: date,
        until: date,
        *,
        cap: int,
        first_batch: int,
        max_probes: int,
        max_undatable_run: int,
        skip: Callable[[dict], bool] | None,
    ) -> None:
        self.fetch_flat = fetch_flat
        self.probe_date = probe_date
        self.since = since
        self.until = until
        self.cap = cap
        self.first_batch = first_batch
        self.max_probes = max_probes
        self.max_undatable_run = max_undatable_run
        self.skip = skip
        self.entries: list[dict] = []
        self.ids: list[str] = []
        self.requested = 0  # size of the last flat request (0 = nothing fetched yet)
        self.raw_count = 0  # flat entries in the last fetch, BEFORE `skip`
        self.known: dict[str, date | None] = {}
        self.bad: set[str] = set()
        self.dirty = False  # an inversion was resolved: the running search is void
        self.probes = 0
        self.undatable_run = 0
        self.flat_requests = 0
        self.truncated = False

    # -- bookkeeping ---------------------------------------------------------

    def fetch(self, n: int) -> None:
        # Clip defensively: `n` is the contract (`playlistend`), and a longer
        # reply would break the "short reply == exhausted" test in `gallop`.
        raw = list(self.fetch_flat(n))[:n]
        self.flat_requests += 1
        self.raw_count = len(raw)
        # `skip` runs AFTER the length is taken, so dropping entries never
        # reads as "the channel is exhausted".
        self.entries = [e for e in raw if not (self.skip and self.skip(e))]
        self.ids = [str(e.get("id") or "") for e in self.entries]
        # A longer (or shifted) list gives known out-of-place runs new neighbours:
        # a run cut off by the previous end of the list may continue into them.
        self._expand_bad()

    def is_bad(self, i: int) -> bool:
        return self.ids[i] in self.bad

    def trusted(self) -> list[tuple[int, date]]:
        """Datable probes of entries not in ``bad``, as ``(index, date)`` by index."""
        out: list[tuple[int, date]] = []
        for i, vid in enumerate(self.ids):
            d = self.known.get(vid) if vid and vid not in self.bad else None
            if d is not None:
                out.append((i, d))
        return out

    def date_at(self, i: int, *, check: bool = True) -> date | None:
        """Publish date of ``entries[i]`` (memoised by id), guarding the order.

        ``check=False`` is for the evidence probes inside :meth:`_resolve`, which
        are judged all together right after.
        """
        vid = self.ids[i]
        if not vid:
            return None  # nothing to look up: undatable, costs no probe
        if vid in self.known:
            return self.known[vid]
        if self.max_probes and self.probes >= self.max_probes:
            raise WindowScanError(
                f"gave up after {self.probes} date probes (limit {self.max_probes}): the "
                "channel's upload order is too irregular to bisect, or dates stopped coming back"
            )
        d = self.probe_date(self.entries[i])
        self.probes += 1
        self.known[vid] = d
        if d is None:
            self.undatable_run += 1
            if self.max_undatable_run and self.undatable_run > self.max_undatable_run:
                raise WindowScanError(
                    f"{self.undatable_run} date probes in a row came back undatable "
                    f"(limit {self.max_undatable_run}): the lookups are probably being refused"
                )
        else:
            self.undatable_run = 0
            if check and vid not in self.bad:
                self._check_order(i, d)
        return d

    def _neighbours(self, i: int) -> tuple[tuple[int, date] | None, tuple[int, date] | None]:
        """Nearest trusted ``(index, date)`` before and after ``i``, excluding ``i``."""
        pred: tuple[int, date] | None = None
        succ: tuple[int, date] | None = None
        for j, dj in self.trusted():
            if j < i:
                pred = (j, dj)
            elif j > i:
                succ = (j, dj)
                break
        return pred, succ

    def _check_order(self, i: int, d: date) -> None:
        # The trusted set is monotone (invariant), so a probe that fits between
        # its two nearest trusted neighbours fits everywhere.
        pred, succ = self._neighbours(i)
        partners = []
        if pred is not None and pred[1] < d:  # an EARLIER index dated OLDER: contradiction
            partners.append(pred[0])
        if succ is not None and d < succ[1]:
            partners.append(succ[0])
        if partners:
            self._resolve(i, partners)

    def _fits(self, i: int, d: date) -> bool:
        pred, succ = self._neighbours(i)
        return (pred is None or pred[1] >= d) and (succ is None or d >= succ[1])

    def _resolve(self, k: int, partners: list[int]) -> None:
        """Classify the inversion exposed by the probe at ``k``.

        Date the two neighbours of ``k`` — a couple of probes — so that a LONE
        outlier is identifiable whichever of the contradicting pair it is: if ``k``
        is the odd one its neighbours disagree with it just as ``partners`` do, and
        if the partner is, ``k`` and its neighbours agree with each other against
        it. Mark the smallest inconsistent set bad (a tie, e.g. two adjacent
        entries, marks both), then grow each bad entry into the run it belongs to.
        """
        self.dirty = True
        for y in (k - 1, k + 1):
            if 0 <= y < len(self.ids) and not self.is_bad(y):
                self.date_at(y, check=False)
        pts = self.trusted()
        for pos in _out_of_order([d for _, d in pts]):
            self.bad.add(self.ids[pts[pos][0]])
        self._expand_bad()

    def _expand_bad(self) -> None:
        """Extend every bad entry outward while its neighbours also break the order.

        A lone outlier stops at once (its neighbours were dated as evidence and
        fit); a stretch of out-of-place entries is dated entry by entry, which the
        probe budget bounds.
        """
        frontier = [i for i in range(len(self.ids)) if self.is_bad(i)]
        while frontier:
            i = frontier.pop()
            for y in (i - 1, i + 1):
                if not (0 <= y < len(self.ids)) or self.is_bad(y):
                    continue
                d = self.date_at(y, check=False)
                if d is not None and not self._fits(y, d):
                    self.bad.add(self.ids[y])
                    frontier.append(y)

    def nearest(self, mid: int, a: int, b: int) -> tuple[int, date] | None:
        """Nearest trusted datable index to ``mid`` inside ``[a, b)``, or ``None``.

        Deterministic outward order (mid, mid+1, mid-1, mid+2, ...). Bad entries
        are skipped without a probe: they are judged by their own date later.
        """
        for off in range(b - a):
            for j in (mid,) if off == 0 else (mid + off, mid - off):
                if a <= j < b and not self.is_bad(j):
                    d = self.date_at(j)
                    if self.dirty:  # the probe exposed an inversion: `run` redoes the search
                        return None
                    if d is not None:
                        return j, d
        return None

    # -- phases --------------------------------------------------------------

    def trusted_near(self, i: int, step: int) -> tuple[int, date] | None:
        """Nearest datable, trusted entry from ``i`` (excluded) in direction ``step``.

        Dates entries on the way if need be (bad ones are skipped, undatable ones
        stepped over — the streak guard bounds that), so it is also how a
        neighbour gets compared against its own neighbours by ``date_at``.
        """
        stop = len(self.entries) if step > 0 else -1
        for j in range(i + step, stop, step):
            if not self.is_bad(j):
                d = self.date_at(j)
                if d is not None and not self.is_bad(j):
                    return j, d
        return None

    def stop_confirmed(self) -> bool:
        """Whether the list provably reaches past ``since``, so the gallop may stop.

        The oldest trusted entry must predate ``since`` AND so must the trusted
        entry before it. One old date at the end of a batch is not proof on its
        own: a single out-of-place entry dated long ago would stop the gallop
        there and hide every in-window video beyond it, with no probe left to
        contradict it. Two old entries in a row mean the list really ran past
        ``since`` (a lone outlier has in-window neighbours). When the end of a
        batch happens to be exactly where the window ends the check fails and the
        gallop just fetches one more batch — a rare, cheap miss.
        """
        bad_before = len(self.bad)
        confirmed = False
        last = self.trusted_near(len(self.entries), -1)
        if last is not None and last[1] < self.since:
            before = self.trusted_near(last[0], -1)
            confirmed = before is not None and before[1] < self.since
        if len(self.bad) != bad_before:  # a probe exposed an inversion: judge again
            return self.stop_confirmed()
        return confirmed

    def gallop(self) -> bool:
        """Grow the flat scan (x2) until it provably reaches past ``since``.

        A reply shorter than asked means the channel is exhausted. Stops at
        ``cap`` with ``truncated`` set when the window was still open. Resumable:
        ``run`` calls it again once the search has settled, because an entry the
        stop relied on may have been marked bad meanwhile. Returns whether it
        fetched anything (the list changed).
        """
        fetched = False
        self.truncated = False
        if not self.requested:
            self.requested = self.first_batch if not self.cap else min(self.first_batch, self.cap)
            self.fetch(self.requested)
            fetched = True
        while self.raw_count >= self.requested:
            if self.stop_confirmed():
                break
            if self.cap and self.requested >= self.cap:
                self.truncated = True
                break
            double = self.requested * 2
            self.requested = double if not self.cap else min(double, self.cap)
            self.fetch(self.requested)
            fetched = True
        return fetched

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
        while a < b and not self.dirty:
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
        """``(hi, lo)``: the window is ``entries[hi:lo]`` (newest first).

        Each border is confirmed with ONE probe just outside it, because that is
        where a lone out-of-place entry does its damage without any probe to
        contradict it: an old date AT ``lo`` makes the search end there and drops
        the in-window entries after it (the entry after ``lo`` would be newer:
        caught), and a new date at ``hi - 1`` pushes the start past in-window
        entries before it (the entry before it would be older: caught). Both
        probes are free when the entry was dated already.
        """
        lo = self.first(lambda d: d < self.since, blind_at_end=True)
        if not self.dirty and lo < len(self.entries):
            self.trusted_near(lo, +1)
        if self.dirty:
            return 0, 0
        if lo == 0:  # even the newest entry is older than `since`: no need to find hi
            return 0, 0
        hi = self.first(lambda d: d <= self.until, blind_at_end=False, edge=True)
        if not self.dirty and hi > 0:
            self.trusted_near(hi - 1, -1)
        return min(hi, lo), lo

    def run(self) -> tuple[int, int]:
        self.gallop()
        while True:
            self.dirty = False
            hi, lo = self.bounds()
            if self.dirty:  # an inversion was resolved mid-search: redo it without it
                continue
            # The gallop's stop may rest on an entry that has just been marked bad,
            # or the borders may have moved: re-prove it, fetching more if it fails.
            if self.gallop() or self.dirty:
                continue
            return hi, lo

    def window(self, hi: int, lo: int) -> list[dict]:
        """Entries inside the window, newest first, each carrying its known ``_date``."""
        out: list[dict] = []
        for k, e in enumerate(self.entries):
            vid = self.ids[k]
            d = self.known.get(vid) if vid else None
            if vid in self.bad and d is not None:
                inside = self.since <= d <= self.until  # out of line: its own date decides
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
    max_probes: int = 200,
    max_undatable_run: int = 24,
    skip: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], dict]:
    """Entries of a newest-first channel list published in ``[since, until]``.

    PURE: all I/O is injected (the same idea as ``tokens.split_by_budget`` taking
    its counter), so the search is testable with plain lists of dates. Entries are
    yt-dlp flat entries: dicts whose ``"id"`` is the video id (the memo key).

    * ``fetch_flat(n)`` -> the first ``n`` flat entries, newest first; fewer than
      ``n`` means the channel is exhausted.
    * ``probe_date(entry)`` -> the exact publish date, or ``None`` when it can't
      be had (private/removed). The expensive call — this function exists to
      make as few of them as possible.
    * ``since``/``until`` are INCLUSIVE: a video published exactly on either
      day is in. ``since > until`` is an empty window and does no I/O.
    * ``cap`` bounds the flat entries ever requested (``0`` = unlimited).
    * ``skip(entry)`` -> ``True`` drops an entry before the search (e.g. a video
      not published yet). Applied AFTER the exhaustion test, so dropped entries
      never make a full reply look short.
    * ``max_probes`` is a hard budget of ``probe_date`` calls (``0`` = unlimited)
      and ``max_undatable_run`` the longest tolerated streak of probes in a row
      that come back ``None`` (``0`` = unlimited). Either tripping raises
      :class:`WindowScanError` instead of returning a window the search can't
      vouch for — a streak that long means dates are not coming back at all, and
      treating "everything is undatable" as "everything is inside" would hand
      back the whole scan. Small undatable regions (a few removed videos) are
      well under both and keep working as documented below.

    Method: **gallop** — fetch ``first_batch``, date its LAST entry, and double
    the request while the list doesn't provably reach past ``since`` (the
    channel's size is unknown up front and the tab has no cursor): the last
    trusted entry AND the one before it must both be older than ``since``; then
    **bisect** the in-memory list for the window's two borders, confirming each
    with one probe just outside it. A monotonic channel costs O(log N) probes
    (<= 30 for 3000 videos, ~19 on average), never one per video. Probes are
    memoised by video id, so a list that shifts between fetches (a video
    published or removed meanwhile) costs nothing extra and can't misattribute a
    date.

    Newest-first order is an assumption, not a guarantee (premieres and VODs
    can be listed out of place). A probe that contradicts the order is detected
    and classified locally: its neighbours are dated, and the smallest set of
    entries that breaks the order — normally ONE — is judged by its own date
    instead of its position, and ``meta["non_monotonic"]`` is set. A stretch of
    out-of-order entries is dated one by one, which the ``max_probes`` budget
    turns into an error.

    What is guaranteed about ONE out-of-place entry, wherever it sits (a batch
    boundary, a border, the middle):

    * it can no longer make in-window entries go MISSING. Its two dangerous
      spots are covered because a lone entry has ordinary neighbours: an old
      date at the end of a batch can't stop the gallop alone (the entry before
      it must be old too), and an old date at the lower border or a new one at
      the upper border is contradicted by the probe just outside the border.
    * it costs a few extra probes: at most 3 in 97%+ of 3000 random trials,
      and 12 at the worst seen (on a 3000-entry channel, when the search had
      already followed the outlier before it was caught).
    * once it is flagged (``non_monotonic``) the result equals a filter by
      actual date.

    What is NOT guaranteed: (1) an outlier no probe contradicts is judged by its
    position, so it can slip IN as an extra (or be absent when its own date says
    it belongs) — callers that date the returned items anyway should re-filter
    them by date, as ``cli`` does with ``--dates``; (2) an outlier that was
    probed but agrees with the probes around it can still push a border
    outward, adding in-order extras — never removing any; (3) with SEVERAL
    outliers the guarantee above holds only when they are not each other's
    confirmation: two adjacent old entries at a batch boundary, or an outlier at
    a border with a second one at its confirming neighbour, still look like a
    real end and can hide in-window entries.

    Undatable entries are routed around (nearest datable neighbour); a region
    with no datable entry at all is kept INSIDE the window, never dropped. An
    entry with no ``"id"`` is undatable by construction (no probe is spent).

    Every returned entry whose date was learned carries it as ``entry["_date"]``
    (a ``date``) — already paid for, so callers needn't fetch it again.

    ``meta``: ``probes`` (``probe_date`` calls), ``flat_requests``, ``scanned``
    (flat entries in the final fetch, before ``skip``), ``truncated_by_cap``
    (the cap stopped the gallop while the window was still open — the result may
    be incomplete) and ``non_monotonic``.
    """
    if first_batch < 1:
        raise ValueError(f"first_batch must be >= 1, got {first_batch}")
    if cap < 0:
        raise ValueError(f"cap must be >= 0 (0 = unlimited), got {cap}")
    if max_probes < 0:
        raise ValueError(f"max_probes must be >= 0 (0 = unlimited), got {max_probes}")
    if max_undatable_run < 0:
        raise ValueError(f"max_undatable_run must be >= 0 (0 = unlimited), got {max_undatable_run}")
    meta = {
        "probes": 0,
        "flat_requests": 0,
        "scanned": 0,
        "truncated_by_cap": False,
        "non_monotonic": False,
    }
    if since > until:
        return [], meta
    scan = _WindowScan(
        fetch_flat,
        probe_date,
        since,
        until,
        cap=cap,
        first_batch=first_batch,
        max_probes=max_probes,
        max_undatable_run=max_undatable_run,
        skip=skip,
    )
    hi, lo = scan.run()
    items = scan.window(hi, lo)
    meta.update(
        probes=scan.probes,
        flat_requests=scan.flat_requests,
        scanned=scan.raw_count,
        truncated_by_cap=scan.truncated,
        non_monotonic=bool(scan.bad),
    )
    return items, meta


def video_upload_date(video_id: str) -> date | None:
    """Exact publish date of one video (UTC), via a full extraction (~1.3 s).

    Metadata only, so the extraction runs with ``ignore_no_formats_error``
    (yt-dlp 2026.07, checked against live videos): without it every video whose
    formats can't be listed raises — members-only ("available to this channel's
    members"), age-gated ("Sign in to confirm your age"), private, an
    unavailable id, an upcoming premiere/live ("This live event will begin in
    ...") — and with it every one of those returns an info dict instead.
    Members-only and age-gated videos then carry their real ``upload_date``; a
    private or removed id has none (-> ``None``).

    ``None`` when there is no usable date: missing/unparseable, or the video is
    ``is_upcoming`` — its ``upload_date`` is then the day the event page was
    CREATED, not a publication (1-3 days before the scheduled start in the live
    check), and an unpublished video belongs to no window.

    Still raises for what the flag can't cover: a network failure, an HTTP
    error, or any extractor error that isn't about formats. The caller tells a
    per-video error from a blanket one (see :func:`_is_per_video_error`);
    :func:`fill_dates` just skips the video.
    """
    info = _extract(
        f"https://www.youtube.com/watch?v={video_id}", {"ignore_no_formats_error": True}
    )
    if info.get("live_status") == "is_upcoming":
        return None
    raw = str(info.get("upload_date") or "")
    if len(raw) != 8 or not (raw.isascii() and raw.isdigit()):
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None


def _is_per_video_error(exc: BaseException) -> bool:
    """Whether ``exc`` is yt-dlp complaining about THIS video, not about the network.

    yt-dlp wraps the original in a ``DownloadError`` (``exc_info[1]``). Observed
    on 2026.07: a private, removed, members-only, age-gated or premiere video
    fails with an ``ExtractorError`` that is ``expected`` and has no ``cause`` —
    a verdict about that video alone. A refused connection (``ProxyError``) or
    an HTTP failure (``HTTPError``, 404 on a tab) is a ``yt_dlp.networking``
    exception, not an ``ExtractorError`` at all, so it says something about
    every video. Not observed, but possible: an ``expected`` ``ExtractorError``
    that is really blanket (a bot wall would be one) passes for per-video here;
    :func:`scan_window`'s undatable-streak guard is what catches that.
    ``yt_dlp`` is imported lazily, here, only once an exception exists.
    """
    try:
        from yt_dlp.utils import ExtractorError
    except ImportError:  # pragma: no cover - yt-dlp is a base dependency
        return False
    info = getattr(exc, "exc_info", None)
    inner = info[1] if isinstance(info, tuple) and len(info) > 1 and info[1] is not None else exc
    return isinstance(inner, ExtractorError) and bool(inner.expected) and inner.cause is None


def _is_unpublished(entry: dict) -> bool:
    """Flat entry of a video that is scheduled but not out yet (premiere / upcoming live)."""
    return entry.get("live_status") == "is_upcoming"


# Consecutive date probes failing for reasons that are not about the video
# (network, rate limit, ...) before `channel_entries_window` stops and re-raises.
_PROBE_BREAKER = 3


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
    * anything else — ``ValueError``.

    ``since > until`` is a ``ValueError`` too: the pure :func:`scan_window` reads
    it as an empty window and does no I/O, which would come back here as
    ``scanned == 0`` — the signal defined below as "the tab itself was empty,
    fall back to Selenium". A caller bug must not look like an engine failure
    (``cli`` already rejects the inverted range before it gets here). All three
    are raised before any network call.

    Videos that are scheduled but not out yet (flat ``live_status`` is
    ``"is_upcoming"``) are dropped from the scan: not published, so in no window,
    and they sit exactly at index 0 where the "until = today" search looks first.

    Same error contract as :func:`channel_entries`: a failing listing request
    is NOT caught — a raised error is the caller's cue to fall back to Selenium,
    as is :class:`WindowScanError` (the search gave up). A returned empty
    ``items`` is ambiguous on its own: ``meta["scanned"] == 0`` means the tab
    itself came back empty (fall back), while ``scanned > 0`` means the window
    genuinely holds no videos.

    A date probe that fails is judged by what failed (:func:`_is_per_video_error`):
    an error about that video (private, removed, ...) just makes it undatable,
    so one unreadable video no longer sinks the scan. Anything else — network,
    rate limit — is also tolerated for the first two in a row, but the third
    consecutive one is re-raised: swallowing a blanket outage would make every
    entry look undatable.
    """
    if order == "popular":
        raise NotImplementedError(
            "a date window can't be combined with order='popular' "
            "(yt-dlp can't list popular uploads) — use the Selenium fallback"
        )
    if order not in ("recent", "old"):
        raise ValueError(f"unknown order: {order!r} (expected recent|old)")
    if since > until:
        raise ValueError(f"since ({since}) is after until ({until}): the window is empty")

    name = ""
    failures = 0  # consecutive probes that failed for a reason that isn't the video's

    def fetch_flat(n: int) -> list[dict]:
        nonlocal name
        info = _extract(
            f"https://www.youtube.com/{ref}/videos",
            {"extract_flat": "in_playlist", "playlistend": n},
        )
        name = name or info.get("channel") or info.get("uploader") or ""
        return list(info.get("entries") or [])

    def probe(entry: dict) -> date | None:
        nonlocal failures
        vid = entry.get("id") or ""
        if not vid:
            return None
        try:
            d = video_upload_date(vid)
        except Exception as exc:
            if not _is_per_video_error(exc):
                failures += 1
                if failures >= _PROBE_BREAKER:
                    raise
            else:
                failures = 0  # YouTube answered, about this video only
            return None
        failures = 0
        return d

    entries, meta = scan_window(fetch_flat, probe, since, until, cap=cap, skip=_is_unpublished)
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
    batch, but never silently: a single warning (logger ``viewlyt``) gives how
    many items stayed undated and, when requests failed, the first error.

    A network outage would otherwise cost every remaining video its full
    timeout (``--dates`` on a 200-video window is ~50 timeouts of wall time
    before the one warning). So the same classification as the window probe
    applies (:func:`_is_per_video_error`): after ``_PROBE_BREAKER`` network-level
    (or unrecognised) errors IN A ROW — a per-video verdict or a success resets
    the count — no new request is started. Requests already running finish; the
    rest stay undated and the warning says how many were not attempted.
    Items without a ``video_id`` are skipped. ``items`` keeps its order;
    ``"date"`` is written from the calling thread, never from a worker.
    """
    todo = [it for it in items if not it.get("date") and it.get("video_id")]
    if not todo:
        return 0

    lock = threading.Lock()
    stop = threading.Event()
    in_a_row = 0  # consecutive network-level failures, across all workers

    def one(item: dict) -> tuple[date | None, Exception | None, bool]:
        """``(date, error, attempted)``; ``attempted`` is False once the breaker tripped."""
        nonlocal in_a_row
        if stop.is_set():
            return None, None, False
        try:
            d = video_upload_date(item["video_id"])
        except Exception as exc:  # one bad video must not sink the rest
            with lock:
                in_a_row = 0 if _is_per_video_error(exc) else in_a_row + 1
                if in_a_row >= _PROBE_BREAKER:
                    stop.set()
            return None, exc, True
        with lock:
            in_a_row = 0
        return d, None, True

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
        results = list(pool.map(one, todo))
    filled = 0
    errors: list[Exception] = []
    not_attempted = 0
    for item, (d, err, attempted) in zip(todo, results, strict=True):
        if err is not None:
            errors.append(err)
        if not attempted:
            not_attempted += 1
        if d is not None:
            item["date"] = d.isoformat()
            filled += 1
    if filled < len(todo):
        why = []
        if errors:
            why.append(f"{len(errors)} request(s) failed, first: {str(errors[0])[:150]}")
        elif not not_attempted:
            why.append("yt-dlp returned no publish date")
        if not_attempted:
            why.append(
                f"stopped after {_PROBE_BREAKER} network-level errors in a row, "
                f"{not_attempted} video(s) not attempted"
            )
        log.warning(
            "could not date %d of %d video(s) (%s)", len(todo) - filled, len(todo), "; ".join(why)
        )
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
    Never raises: any non-dict/garbage input yields ``[]``; a ``utf8`` that
    isn't a string is ignored, and a ``tStartMs`` that isn't a finite number
    (``json.loads`` accepts ``NaN``/``Infinity``) just leaves the timestamp empty.
    """
    from .htmltext import format_duration

    events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(events, list):
        return []
    segments: list[tuple[str, str]] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("aAppend") or not isinstance(ev.get("segs"), list):
            continue
        raw = "".join(
            s["utf8"] for s in ev["segs"] if isinstance(s, dict) and isinstance(s.get("utf8"), str)
        )
        text = " ".join(raw.split())
        if not text:
            continue
        start = ev.get("tStartMs")
        ts = ""
        if isinstance(start, (int, float)) and not isinstance(start, bool):
            # An int can't be NaN/inf, and math.isfinite() would overflow on a huge one.
            if isinstance(start, int) or math.isfinite(start):
                ts = format_duration(int(start) // 1000)
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


def _primary(tag: str) -> str:
    """Primary language subtag, lower-cased: ``pt-BR`` -> ``pt``, ``zh_Hans`` -> ``zh``."""
    return tag.replace("_", "-").split("-", 1)[0].lower()


def _in_language(tracks: dict, language: str, *, suffix: str = "") -> list[object]:
    """Track lists of ``tracks`` in ``language``, best first, ``live_chat`` never among them.

    The exact key (``language + suffix``) leads, then every key of the same
    primary language (``pt`` ~ ``pt-BR`` ~ ``pt-PT``) in sorted order — creators
    file a ``pt-BR`` track under a video YouTube calls plain ``pt``, and the
    reverse, so an exact match alone would let ASR beat the human track. Sorting
    makes the choice among regional variants deterministic. ``suffix`` (``"-orig"``)
    restricts to — and strips from — that kind of key.
    """
    primary = _primary(language)
    keys = sorted(
        k
        for k in tracks
        if isinstance(k, str)
        and k != "live_chat"
        and k.endswith(suffix)
        and k != suffix
        and _primary(k[: len(k) - len(suffix)]) == primary
    )
    exact = language + suffix
    keys.sort(key=lambda k: k != exact)  # stable: the exact key first, the rest stay sorted
    return [tracks[k] for k in keys]


def _pick_caption_track(info: dict) -> str | None:
    """``json3`` URL of the track the YouTube transcript panel shows by default.

    Pure. The panel prefers a human-made track in the video's own language, then
    the ASR (auto-generated) track of that same language. Languages match by
    PRIMARY subtag (``pt`` ~ ``pt-BR``), the exact key first and regional
    variants after it in sorted order. Priority:

    1. manual ``subtitles`` in ``language``;
    2. ASR ``automatic_captions["<language>-orig"]`` (yt-dlp keys the ORIGINAL
       ASR track with ``-orig``; the plain key can be a re-translation);
    3. ASR ``automatic_captions[language]``;
    4. ``language`` missing or matching nothing: the language is read off the
       ASR ``-orig`` key(s) — that IS the video's spoken language — and a manual
       track in it wins over the ASR track, otherwise the ASR ``-orig`` track;
    5. the first manual track that isn't ``live_chat`` (a replay's chat log is
       filed under ``subtitles`` but is not a caption).

    A track with no ``json3`` entry is skipped (next candidate). ``None`` when
    nothing qualifies — in particular a translated ASR track is NEVER a last
    resort: ASR translations (~150 of them per video) would be machine output
    of machine output, worse than the Selenium panel fallback.
    """
    subs = info.get("subtitles")
    auto = info.get("automatic_captions")
    subs = subs if isinstance(subs, dict) else {}
    auto = auto if isinstance(auto, dict) else {}
    language = info.get("language")

    candidates: list[object] = []
    if language and isinstance(language, str):
        candidates += _in_language(subs, language)
        candidates += _in_language(auto, language, suffix="-orig")
        candidates.append(auto.get(language))
    for key in auto:
        if isinstance(key, str) and key.endswith("-orig") and len(key) > len("-orig"):
            candidates += _in_language(subs, key[: -len("-orig")])
            candidates.append(auto[key])
    candidates += [v for k, v in subs.items() if k != "live_chat"]
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

    The first pass runs with ``ignore_no_formats_error``: captions don't need
    formats, and an age-gated video fails on its formats yet still lists its
    caption tracks (live check: 14 manual + 157 ASR on one, where it used to
    raise). That flag
    also turns "unavailable", "members-only" and "upcoming" into an empty info
    dict, which would silently change this function's contract (a removed video
    must RAISE, so ``--transcript-engine ytdlp`` fails it instead of reporting a
    fake ``youtube video #id`` title with no segments). So when the first pass
    found no captions AND no formats, the video is asked again strictly, and
    whatever that raises is what the caller gets, exactly as before the flag.
    """
    import json

    import yt_dlp

    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {**_BASE_OPTS, "logger": _NullLogger()}
    with yt_dlp.YoutubeDL({**opts, "ignore_no_formats_error": True}) as ydl:
        info = ydl.extract_info(url, download=False) or {}
        track = _pick_caption_track(info)
        if track:
            return info, json.loads(ydl.urlopen(track).read())
    if info.get("formats"):  # an ordinary video that simply has no captions
        return info, None
    with yt_dlp.YoutubeDL(opts) as ydl:  # the flag may have swallowed a real failure
        info = ydl.extract_info(url, download=False) or {}
        track = _pick_caption_track(info)
        payload = json.loads(ydl.urlopen(track).read()) if track else None
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
