"""Browser-free, network-free tests for the channel-window core.

Covers the pure pieces (``htmltext.parse_window_spec``, the optional ``date`` in
``format_related``), the injected-I/O search (``ytdl.scan_window``) and the
yt-dlp wrappers around it (``video_upload_date``, ``channel_entries_window``,
``fill_dates``), the latter through a fake of ``ytdl._extract`` that honours
``playlistend`` — yt-dlp itself is never imported.
"""

from __future__ import annotations

import logging
import os
import random
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, timedelta

import pytest
from conftest import SRC

from viewlyt import ytdl
from viewlyt.htmltext import format_related, parse_window_spec
from viewlyt.ytdl import channel_entries_window, fill_dates, scan_window, video_upload_date

TODAY = date(2026, 7, 15)
NEWEST = date(2026, 6, 30)


# ---------------------------------------------------------------------------
# parse_window_spec
# ---------------------------------------------------------------------------


def test_parse_window_spec_absolute_ignores_today() -> None:
    assert parse_window_spec("2026-03-15", TODAY) == date(2026, 3, 15)
    assert parse_window_spec("2030-01-01", TODAY) == date(2030, 1, 1)  # future is the caller's call


@pytest.mark.parametrize(
    ("spec", "days"),
    [("10d", 10), ("2w", 14), ("3m", 90), ("1y", 365), ("12w", 84), ("100d", 100)],
)
def test_parse_window_spec_relative_units(spec: str, days: int) -> None:
    # Same approximation as parse_relative_date: months = 30d, years = 365d.
    assert parse_window_spec(spec, TODAY) == TODAY - timedelta(days=days)


@pytest.mark.parametrize("spec", [" 3W ", "\t10D\n", "3w", "3W", "  2026-03-15 ", "\n1Y"])
def test_parse_window_spec_tolerates_whitespace_and_unit_case(spec: str) -> None:
    assert parse_window_spec(spec, TODAY) == parse_window_spec(spec.strip().lower(), TODAY)


@pytest.mark.parametrize("spec", ["0d", "0w", "0m", "0y", " 0D "])
def test_parse_window_spec_zero_is_today(spec: str) -> None:
    assert parse_window_spec(spec, TODAY) == TODAY


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "0x",
        "-3d",
        "+3d",
        "90",
        "3q",
        "d",
        "w3",
        "3 d",
        "1.5d",
        "3dd",
        "2026-02-30",  # not a calendar date
        "2026-13-01",
        "2026-00-10",
        "2026-3-1",
        "20260301",
        "2026/03/01",
        "2026-03-01T00:00",
        "٣d",  # non-ASCII digit
        "99999999999999999999y",  # int overflow in timedelta
        "9999y",  # reaches before year 1
        "tomorrow",
    ],
)
def test_parse_window_spec_rejects_garbage(spec: str) -> None:
    with pytest.raises(ValueError, match="yyyy-mm-dd"):
        parse_window_spec(spec, TODAY)


# ---------------------------------------------------------------------------
# format_related: optional "date"
# ---------------------------------------------------------------------------


def test_format_related_with_date() -> None:
    items = [{"title": "T", "views": "1.2M views", "date": "2026-05-03", "url": "u"}]
    assert format_related(items) == ["1. [1.2M views. 2026-05-03. T](u)"]


def test_format_related_date_with_exact_views_int() -> None:
    items = [{"title": "T", "views": "", "views_int": 1234567, "date": "2026-05-03", "url": "u"}]
    assert format_related(items) == ["1. [1,234,567 views. 2026-05-03. T](u)"]


def test_format_related_date_with_empty_views() -> None:
    items = [{"title": "T", "views": "", "date": "2026-05-03", "url": "u"}]
    assert format_related(items) == ["1. [2026-05-03. T](u)"]
    items = [{"title": "T", "views": "", "views_int": None, "date": "2026-05-03", "url": "u"}]
    assert format_related(items) == ["1. [2026-05-03. T](u)"]


def test_format_related_mixed_dated_and_undated_items() -> None:
    items = [
        {"title": "A", "views": "1 views", "date": "2026-05-03", "url": "ua"},
        {"title": "B", "views": "2 views", "url": "ub"},  # no date key
        {"title": "C", "views": "3 views", "date": "", "url": "uc"},  # empty date == absent
        {"title": "D", "views": "", "date": None, "url": "ud"},  # None date == absent
        {"title": "E", "views": "", "date": "2026-01-02", "url": "ue"},
    ]
    assert format_related(items) == [
        "1. [1 views. 2026-05-03. A](ua)",
        "2. [2 views. B](ub)",
        "3. [3 views. C](uc)",
        "4. [D](ud)",
        "5. [2026-01-02. E](ue)",
    ]


def test_format_related_date_keeps_numbering_contiguous_over_skipped_titles() -> None:
    items = [
        {"title": "", "views": "9 views", "date": "2026-05-03", "url": "u0"},  # skipped
        {"title": "T", "views": "", "date": "2026-05-04", "url": "u1"},
    ]
    assert format_related(items) == ["1. [2026-05-04. T](u1)"]


def test_format_related_without_date_is_byte_identical_to_the_legacy_output() -> None:
    # Locks the contract that every existing caller (related sidebar, plain
    # channel listing) renders EXACTLY as before the optional "date" existed.
    items = [
        {
            "title": "Vídeo A",
            "views": "1.2B views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        },
        {"title": "  multi\nline  ", "views": "20M views", "url": "u2"},
        {"title": "No count", "views": "", "url": "u3"},
        {"title": "", "views": "5 views", "url": "u4"},
        {"title": "Exact", "views": "1.2M views", "views_int": 1234567, "url": "u5"},
        {"title": "Rounded", "views": "1.2M views", "views_int": None, "url": "u6"},
        {"title": "Spaces", "views": "  3   views ", "url": "  u7  "},
    ]
    assert format_related(items) == [
        "1. [1.2B views. Vídeo A](https://www.youtube.com/watch?v=aaaaaaaaaaa)",
        "2. [20M views. multi line](u2)",
        "3. [No count](u3)",
        "4. [1,234,567 views. Exact](u5)",
        "5. [1.2M views. Rounded](u6)",
        "6. [3 views. Spaces](u7)",
    ]
    assert format_related([]) == []


# ---------------------------------------------------------------------------
# scan_window: fakes built from a plain list of dates
# ---------------------------------------------------------------------------


def daily(n: int, start: date = NEWEST) -> list[date]:
    """``n`` strictly newest-first dates, one per day: index i is ``start - i`` days."""
    return [start - timedelta(days=i) for i in range(n)]


def day(i: int) -> date:
    """The date of index ``i`` in a :func:`daily` channel."""
    return NEWEST - timedelta(days=i)


class FakeChannel:
    """``fetch_flat``/``probe_date`` over a list of dates, counting every call."""

    def __init__(self, dates: list[date | None]) -> None:
        self.dates = list(dates)
        self.requests: list[int] = []
        self.probed: list[int] = []

    def fetch_flat(self, n: int) -> list[dict]:
        self.requests.append(n)
        return [{"id": f"v{i}", "idx": i} for i in range(min(n, len(self.dates)))]

    def probe_date(self, entry: dict) -> date | None:
        self.probed.append(entry["idx"])
        return self.dates[entry["idx"]]

    def scan(self, since: date, until: date, **kw) -> tuple[list[int], dict, list[dict]]:
        items, meta = scan_window(self.fetch_flat, self.probe_date, since, until, **kw)
        return [e["idx"] for e in items], meta, items


def brute(dates: list[date | None], since: date, until: date) -> list[int]:
    return [i for i, d in enumerate(dates) if d is not None and since <= d <= until]


def test_scan_meta_has_exactly_the_documented_keys() -> None:
    ch = FakeChannel(daily(10))
    _, meta, _ = ch.scan(day(5), day(1))
    assert set(meta) == {"probes", "flat_requests", "scanned", "truncated_by_cap", "non_monotonic"}
    assert meta["probes"] == len(ch.probed)
    assert meta["flat_requests"] == len(ch.requests)


def test_scan_since_after_until_is_empty_and_does_no_io() -> None:
    ch = FakeChannel(daily(100))
    got, meta, _ = ch.scan(day(10), day(40))  # since (day 10) is NEWER than until (day 40)
    assert got == []
    assert ch.requests == [] and ch.probed == []
    assert meta == {
        "probes": 0,
        "flat_requests": 0,
        "scanned": 0,
        "truncated_by_cap": False,
        "non_monotonic": False,
    }


def test_scan_empty_channel() -> None:
    ch = FakeChannel([])
    got, meta, _ = ch.scan(day(30), day(0))
    assert got == []
    assert meta["scanned"] == 0 and meta["probes"] == 0 and meta["truncated_by_cap"] is False


def test_scan_window_inside_a_gap_is_empty_but_scanned() -> None:
    dates = daily(50) + [date(2025, 12, 31) - timedelta(days=i) for i in range(50)]
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(date(2026, 2, 1), date(2026, 3, 1))  # nothing was published then
    assert got == []
    assert meta["scanned"] > 0  # a genuinely empty window, not an empty tab
    assert meta["non_monotonic"] is False


def test_scan_window_newer_than_everything_is_empty() -> None:
    ch = FakeChannel(daily(200))
    got, meta, _ = ch.scan(NEWEST + timedelta(days=5), NEWEST + timedelta(days=50))
    assert got == []
    assert meta["truncated_by_cap"] is False


def test_scan_window_older_than_everything_is_empty() -> None:
    ch = FakeChannel(daily(200))
    got, _, _ = ch.scan(date(1990, 1, 1), date(2000, 1, 1))
    assert got == []


def test_scan_window_covers_the_whole_channel() -> None:
    ch = FakeChannel(daily(300))
    got, meta, _ = ch.scan(date(2000, 1, 1), date(2100, 1, 1))
    assert got == list(range(300))
    assert meta["truncated_by_cap"] is False
    assert meta["scanned"] == 300
    # The gallop: 64, doubling, until a reply comes back shorter than asked.
    assert ch.requests == [64, 128, 256, 512]


def test_scan_borders_are_inclusive() -> None:
    dates = daily(100)
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(since=day(40), until=day(10))  # exactly the dates of entries 40 and 10
    assert got == list(range(10, 41))
    assert 10 in got and 40 in got
    assert 9 not in got and 41 not in got


def test_scan_borders_inclusive_with_ties_on_the_border_days() -> None:
    dates = [NEWEST - timedelta(days=i // 3) for i in range(150)]  # three videos per day
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(since=NEWEST - timedelta(days=20), until=NEWEST - timedelta(days=10))
    assert got == brute(dates, NEWEST - timedelta(days=20), NEWEST - timedelta(days=10))
    assert got == list(range(30, 63))  # all three of day 10 AND all three of day 20
    got, _, _ = ch.scan(since=NEWEST - timedelta(days=20), until=NEWEST - timedelta(days=20))
    assert got == [60, 61, 62]  # a single-day window keeps every video of that day


def test_scan_single_day_window_and_since_equals_until() -> None:
    ch = FakeChannel(daily(100))
    got, _, _ = ch.scan(day(33), day(33))
    assert got == [33]


def test_scan_channel_smaller_than_first_batch() -> None:
    dates = daily(10)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(7), day(2))
    assert got == list(range(2, 8))
    assert ch.requests == [64]  # one fetch: a short reply already means "exhausted"
    assert meta["flat_requests"] == 1 and meta["scanned"] == 10


def test_scan_until_in_the_past_needs_the_newer_border_too() -> None:
    dates = daily(500)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since=day(300), until=day(200))
    assert got == list(range(200, 301))
    assert meta["probes"] < 30


def test_scan_until_today_skips_the_second_bisection() -> None:
    # `until` at/after the newest video: probing index 0 settles the newer
    # border, so the cost is the older border's bisection alone.
    ch = FakeChannel(daily(3000))
    _, only_since, _ = ch.scan(day(500), NEWEST + timedelta(days=30))
    ch2 = FakeChannel(daily(3000))
    _, both, _ = ch2.scan(day(500), day(100))
    assert only_since["probes"] < both["probes"]


def test_scan_custom_first_batch() -> None:
    ch = FakeChannel(daily(100))
    got, _, _ = ch.scan(day(60), day(0), first_batch=10)
    assert got == list(range(61))
    assert ch.requests == [10, 20, 40, 80]  # 80th reply (index 79) is already older than day 60

    ch = FakeChannel(daily(100))
    got, _, _ = ch.scan(day(60), day(0), first_batch=1)
    assert got == list(range(61))
    assert ch.requests[:4] == [1, 2, 4, 8]


def test_scan_rejects_bad_arguments() -> None:
    ch = FakeChannel(daily(10))
    with pytest.raises(ValueError, match="first_batch"):
        ch.scan(day(5), day(1), first_batch=0)
    with pytest.raises(ValueError, match="cap"):
        ch.scan(day(5), day(1), cap=-1)


def test_scan_clips_a_reply_longer_than_asked() -> None:
    dates = daily(300)
    asked: list[int] = []

    def greedy(n: int) -> list[dict]:  # ignores `n`, returns everything
        asked.append(n)
        return [{"id": f"v{i}", "idx": i} for i in range(300)]

    items, meta = scan_window(greedy, lambda e: dates[e["idx"]], day(200), day(0))
    assert [e["idx"] for e in items] == list(range(201))
    assert asked == [64, 128, 256]
    assert meta["scanned"] == 256


# --- cap -------------------------------------------------------------------


def test_scan_cap_hit_with_window_still_open_is_truncated() -> None:
    dates = daily(1000)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(999), day(0), cap=128)
    assert got == list(range(128))  # everything scanned is in; the rest was never looked at
    assert meta["truncated_by_cap"] is True
    assert meta["scanned"] == 128
    assert ch.requests == [64, 128]


def test_scan_cap_hit_but_window_already_closed_is_not_truncated() -> None:
    dates = daily(1000)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(100), day(0), cap=128)  # entry 127 is already older than since
    assert got == list(range(101))
    assert meta["truncated_by_cap"] is False
    assert ch.requests == [64, 128]


def test_scan_cap_smaller_than_first_batch_and_not_a_power_of_two() -> None:
    ch = FakeChannel(daily(1000))
    got, meta, _ = ch.scan(day(999), day(0), cap=10)
    assert ch.requests == [10]
    assert got == list(range(10)) and meta["truncated_by_cap"] is True

    ch = FakeChannel(daily(1000))
    got, meta, _ = ch.scan(day(999), day(0), cap=100)
    assert ch.requests == [64, 100]  # the last step is clamped to the cap, never past it
    assert got == list(range(100)) and meta["truncated_by_cap"] is True


def test_scan_cap_equal_to_channel_size_and_window_open() -> None:
    # The cap can't tell "exactly cap videos" from "more beyond": honest answer is
    # "may be incomplete", never a silent False.
    ch = FakeChannel(daily(128))
    got, meta, _ = ch.scan(day(999), day(0), cap=128)
    assert got == list(range(128))
    assert meta["truncated_by_cap"] is True


def test_scan_cap_zero_means_unlimited() -> None:
    dates = daily(6000)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(5999), day(0), cap=0)
    assert len(got) == 6000
    assert meta["truncated_by_cap"] is False and meta["scanned"] == 6000

    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(5999), day(0))  # default cap 5000
    assert len(got) == 5000
    assert meta["truncated_by_cap"] is True
    assert max(ch.requests) == 5000


# --- undatable entries -------------------------------------------------------


def expected_with_blind(dates: list[date | None], since: date, until: date) -> list[int]:
    """The documented contract for a monotone channel with undatable entries.

    The window runs from just after the last datable entry NEWER than ``until``
    to just before the first datable entry OLDER than ``since``: undatable
    entries in between (including a blind region at either border) are kept.
    An inverted window (``since > until``) is empty.
    """
    if since > until:
        return []
    newer = [i for i, d in enumerate(dates) if d is not None and d > until]
    older = [i for i, d in enumerate(dates) if d is not None and d < since]
    hi = newer[-1] + 1 if newer else 0
    lo = older[0] if older else len(dates)
    return list(range(hi, lo)) if hi < lo else []


def test_scan_undatable_entries_are_routed_around() -> None:
    dates: list[date | None] = list(daily(200))
    for i in (3, 20, 63, 70, 100):  # 63 is the first gallop probe, 20 sits inside the window
        dates[i] = None
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(day(50), day(10))
    assert got == list(range(10, 51))  # 20 (undatable, inside) is kept; 3 and 63 are outside


def test_scan_undatable_region_at_the_older_border_is_kept_inside() -> None:
    dates: list[date | None] = list(daily(100))
    for i in range(30, 46):  # 16 undatable entries straddle where `since` would cut
        dates[i] = None
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(since=day(45), until=day(0))
    assert got == list(range(0, 46))  # the whole blind region stays IN, entry 46 (older) is out
    assert 46 not in got


def test_scan_undatable_region_at_the_newer_border_is_kept_inside() -> None:
    dates: list[date | None] = list(daily(100))
    for i in range(0, 6):
        dates[i] = None  # the newest six can't be dated
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(since=day(50), until=day(6))
    assert got == list(range(0, 51))  # kept: unknown beats silently dropped


def test_scan_all_entries_undatable_raises_instead_of_returning_the_whole_scan() -> None:
    # REWRITTEN (was: "all entries undatable -> the whole scan is returned"). When
    # dates stop coming back at all, "everything is undatable" must not read as
    # "everything is inside the window": the streak guard gives up with a clear error.
    ch = FakeChannel([None] * 100)
    with pytest.raises(ytdl.WindowScanError, match="in a row"):
        ch.scan(day(50), day(10))
    assert len(ch.probed) == 25 == len(set(ch.probed))  # limit 24 + the probe that tripped it


def test_scan_all_entries_undatable_with_the_guards_off_keeps_everything() -> None:
    # The old contract, still available when the caller switches the guards off.
    ch = FakeChannel([None] * 100)
    got, meta, items = ch.scan(day(50), day(10), max_probes=0, max_undatable_run=0)
    assert got == list(range(100))
    assert all("_date" not in e for e in items)
    assert len(ch.probed) == len(set(ch.probed))  # still never a repeated probe
    assert meta["non_monotonic"] is False


def test_scan_undatable_streak_guard_boundary_is_the_documented_limit() -> None:
    # 24 undatable probes in a row are tolerated (default limit), the 25th trips it.
    got, _, _ = FakeChannel([None] * 24).scan(day(50), day(10))
    assert got == list(range(24))  # kept inside, no error
    with pytest.raises(ytdl.WindowScanError, match="in a row"):
        FakeChannel([None] * 25).scan(day(50), day(10))
    # The same list passes once the caller widens the guard.
    got, _, _ = FakeChannel([None] * 25).scan(day(50), day(10), max_undatable_run=25)
    assert got == list(range(25))


def test_scan_undatable_streak_guard_is_reset_by_a_datable_probe() -> None:
    # Undatable entries scattered through a channel are never a streak: a datable
    # probe in between resets the counter, however many there are in total.
    dates: list[date | None] = list(daily(300))
    for i in range(0, 300, 2):
        dates[i] = None  # every other entry: 150 undatable in total
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(day(200), day(0))
    assert got == expected_with_blind(dates, day(200), day(0))


def test_scan_undatable_tail_does_not_stop_the_gallop_early() -> None:
    # The last entry of the first batch (index 63) is undatable: the gallop must
    # judge the batch by its nearest datable entry, not give up or crash.
    dates: list[date | None] = list(daily(300))
    dates[63] = dates[62] = None
    ch = FakeChannel(dates)
    got, _, _ = ch.scan(day(200), day(0))
    assert got == list(range(201))


def test_scan_random_undatable_entries_follow_the_documented_contract() -> None:
    rng = random.Random(20260715)
    for trial in range(400):
        n = rng.randint(1, 300)
        dates: list[date | None] = []
        d = NEWEST
        for _ in range(n):
            dates.append(None if rng.random() < rng.choice([0.05, 0.2, 0.6]) else d)
            d -= timedelta(days=rng.randint(0, 3))
        since = NEWEST - timedelta(days=rng.randint(-5, 500))
        until = NEWEST - timedelta(days=rng.randint(-5, 500))
        if rng.random() < 0.6 and since > until:
            since, until = until, since
        ch = FakeChannel(dates)
        got, meta, _ = ch.scan(since, until, first_batch=rng.randint(1, 80))
        assert got == expected_with_blind(dates, since, until), (trial, n, since, until)
        assert len(ch.probed) == len(set(ch.probed)), trial
        assert meta["non_monotonic"] is False


# --- non-monotonic channels ---------------------------------------------------


def test_scan_inversion_caught_by_the_gallop_is_judged_by_its_own_date() -> None:
    dates: list[date | None] = list(daily(40))
    dates[15] = NEWEST + timedelta(days=2)  # a premiere/VOD listed far below where it belongs
    ch = FakeChannel(dates)
    since, until = day(30), day(10)
    got, meta, _ = ch.scan(since, until, first_batch=8)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until)  # entry 15 is positionally inside but dated outside
    assert 15 not in got
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_inversion_caught_by_the_bisection_is_judged_by_its_own_date() -> None:
    dates: list[date | None] = list(daily(100))
    dates[57] = day(90)  # listed at 57 but dated like entry 90: older than its later neighbours
    ch = FakeChannel(dates)
    since, until = day(60), day(5)
    got, meta, _ = ch.scan(since, until, first_batch=200)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until)
    assert 57 not in got
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_inversion_inside_the_window_by_date_is_kept() -> None:
    # The out-of-place entry is dated INSIDE the window: it must stay in, and
    # nothing dated inside the window may be dropped.
    dates: list[date | None] = list(daily(100))
    dates[57] = day(30)  # newer than its neighbours but still inside [day(60), day(5)]
    ch = FakeChannel(dates)
    since, until = day(60), day(5)
    got, meta, _ = ch.scan(since, until, first_batch=200)
    assert got == brute(dates, since, until)
    assert 57 in got
    assert meta["non_monotonic"] is True


def test_scan_monotone_channel_is_not_flagged() -> None:
    ch = FakeChannel([NEWEST - timedelta(days=i // 4) for i in range(400)])  # ties are fine
    _, meta, _ = ch.scan(NEWEST - timedelta(days=80), NEWEST - timedelta(days=20))
    assert meta["non_monotonic"] is False


def test_scan_single_inversion_is_exact_whenever_it_is_detected() -> None:
    # Property: with ONE out-of-place entry, every violating pair includes it, so
    # once it is flagged the hull covers it and the result must equal the brute
    # force over the actual dates. (Undetected cases carry no claim: see the
    # docstring's LIMIT.)
    rng = random.Random(7)
    flagged = 0
    for trial in range(1600):
        n = rng.randint(30, 300)
        dates: list[date | None] = []
        d = NEWEST
        for _ in range(n):
            dates.append(d)
            d -= timedelta(days=rng.randint(0, 2))
        x = rng.randrange(n)
        dates[x] = NEWEST - timedelta(days=rng.randint(-10, 2 * n))
        since = NEWEST - timedelta(days=rng.randint(0, n))
        until = since + timedelta(days=rng.randint(0, n))
        ch = FakeChannel(dates)
        got, meta, _ = ch.scan(since, until, first_batch=rng.randint(1, 80))
        assert len(ch.probed) == len(set(ch.probed)), trial
        if meta["non_monotonic"]:
            flagged += 1
            assert got == brute(dates, since, until), (trial, n, x, since, until)
    assert flagged >= 50  # the property isn't vacuous


# --- probe budget, memoisation, the dates it learned ----------------------------


def test_scan_probe_budget_is_logarithmic_on_a_3000_video_channel() -> None:
    dates = daily(3000)
    rng = random.Random(3)
    windows = [
        (day(2999), day(0)),  # everything
        (day(0), day(0)),  # just the newest
        (day(1500), day(1500)),
        (day(2999), day(2999)),  # just the oldest
        (day(500), day(100)),
        (day(2990), day(2900)),
        (date(2000, 1, 1), date(2100, 1, 1)),
        (date(2100, 1, 1), date(2200, 1, 1)),  # in the future
        (date(1990, 1, 1), date(2000, 1, 1)),  # before the channel existed
    ]
    for _ in range(60):
        a, b = sorted(rng.sample(range(3000), 2))
        windows.append((day(b), day(a)))
    worst = 0
    for since, until in windows:
        ch = FakeChannel(dates)
        got, meta, _ = ch.scan(since, until)
        assert got == brute(dates, since, until)
        assert meta["probes"] == len(ch.probed) == len(set(ch.probed))
        worst = max(worst, meta["probes"])
    assert worst < 40  # the spec's ceiling ("well under 40")
    assert worst <= 30  # what this implementation measures (26-28); regress loudly


def test_scan_probe_count_depends_on_where_the_window_sits_not_on_channel_size() -> None:
    small = FakeChannel(daily(300))
    large = FakeChannel(daily(3000))
    _, m_small, _ = small.scan(day(250), day(100))
    _, m_large, _ = large.scan(day(250), day(100))
    # Ten times the videos, same window: the gallop stops at the same batch.
    assert m_large["probes"] <= m_small["probes"] + 4
    assert m_large["scanned"] == m_small["scanned"] == 256


def test_scan_never_probes_the_same_entry_twice() -> None:
    cases = [
        (daily(3000), day(2000), day(1000)),
        (daily(500), day(499), day(0)),
        ([NEWEST - timedelta(days=i // 5) for i in range(800)], day(60), day(30)),
    ]
    for dates, since, until in cases:
        ch = FakeChannel(dates)
        ch.scan(since, until, first_batch=16)
        assert len(ch.probed) == len(set(ch.probed))


def test_scan_attaches_the_dates_it_learned_and_only_those() -> None:
    dates = daily(2000)
    ch = FakeChannel(dates)
    got, meta, items = ch.scan(day(1500), day(100))
    assert got == list(range(100, 1501))
    dated = [e for e in items if "_date" in e]
    assert dated  # the probes that landed inside the window are not thrown away
    assert len(dated) < len(items)  # ... but the rest was never paid for
    for e in dated:
        assert e["_date"] == dates[e["idx"]] and isinstance(e["_date"], date)
        assert e["idx"] in ch.probed
    assert meta["probes"] > 0
    assert [e["idx"] for e in items] == sorted(e["idx"] for e in items)  # newest first


def test_scan_random_monotone_channels_match_a_brute_force_filter() -> None:
    rng = random.Random(20260715)
    for trial in range(250):
        n = rng.randint(0, 400)
        dates: list[date | None] = []
        d = NEWEST
        for _ in range(n):
            dates.append(d)
            d -= timedelta(days=rng.randint(0, 3))  # 0 => ties, several videos on one day
        since = NEWEST - timedelta(days=rng.randint(-5, 700))
        until = NEWEST - timedelta(days=rng.randint(-5, 700))
        if rng.random() < 0.5 and since > until:
            since, until = until, since
        first_batch = rng.randint(1, 100)
        ch = FakeChannel(dates)
        got, meta, items = ch.scan(since, until, first_batch=first_batch)
        assert got == brute(dates, since, until), (trial, n, since, until, first_batch)
        assert len(ch.probed) == len(set(ch.probed)), trial
        assert meta["probes"] == len(ch.probed)
        assert meta["non_monotonic"] is False
        assert meta["truncated_by_cap"] is False
        for e in items:
            if "_date" in e:
                assert e["_date"] == dates[e["idx"]]


# --- the list shifts while it is being scanned -----------------------------------


class MutatingChannel:
    """A channel that changes UNDER the scan: ``mutations[k](channel)`` runs just before the
    k-th flat fetch (0-based) is served, the way a channel keeps publishing (a video at the
    front) or deleting (one vanishes) while the gallop re-fetches a longer prefix."""

    def __init__(
        self,
        items: list[tuple[str, date]],
        mutations: dict[int, Callable[[MutatingChannel], None]] | None = None,
    ) -> None:
        self.items = list(items)
        self.dates = dict(items)
        self.mutations = mutations or {}
        self.requests: list[int] = []
        self.served: list[str] = []  # ids of the LAST fetch: the list the answer is about
        self.probed: list[str] = []

    def insert(self, pos: int, item: tuple[str, date]) -> None:
        self.items.insert(pos, item)
        self.dates[item[0]] = item[1]

    def remove(self, vid: str) -> None:
        self.items = [it for it in self.items if it[0] != vid]

    def fetch_flat(self, n: int) -> list[dict]:
        k = len(self.requests)
        self.requests.append(n)
        if k in self.mutations:
            self.mutations[k](self)
        out = self.items[:n]
        self.served = [vid for vid, _ in out]
        return [{"id": vid} for vid, _ in out]

    def probe_date(self, entry: dict) -> date | None:
        self.probed.append(entry["id"])
        return self.dates[entry["id"]]

    def check(self, since: date, until: date, **kw) -> tuple[list[str], dict]:
        """Scan, then assert the answer equals the brute-force filter of the FINAL list,
        that no item carries another video's date, and that no video was dated twice."""
        items, meta = scan_window(self.fetch_flat, self.probe_date, since, until, **kw)
        ids = [e["id"] for e in items]
        assert ids == [v for v in self.served if since <= self.dates[v] <= until]
        for e in items:
            if "_date" in e:
                assert e["_date"] == self.dates[e["id"]], e["id"]
        assert len(self.probed) == len(set(self.probed))
        return ids, meta


def numbered(n: int) -> list[tuple[str, date]]:
    return [(f"v{i:03d}", day(i)) for i in range(n)]


def test_scan_survives_a_video_published_between_two_fetches() -> None:
    # A new upload lands at the front before the second fetch: every index shifts by one.
    new = ("new000", NEWEST + timedelta(days=1))
    ch = MutatingChannel(numbered(400), {1: lambda c: c.insert(0, new)})
    ids, meta = ch.check(day(150), NEWEST + timedelta(days=5), first_batch=16)
    assert ids[0] == "new000" and ids[1:] == [f"v{i:03d}" for i in range(151)]
    assert meta["non_monotonic"] is False
    assert len(ch.requests) >= 3  # the scenario really did re-fetch more than once


def test_scan_survives_a_video_removed_between_two_fetches() -> None:
    # One removal of a video the scan never dated, one of a video it already had.
    ch = MutatingChannel(
        numbered(400),
        {1: lambda c: c.remove("v010"), 2: lambda c: c.remove("v015")},
    )
    ids, _ = ch.check(day(150), NEWEST, first_batch=16)
    assert "v010" not in ids and "v015" not in ids and "v016" in ids


def test_scan_survives_a_video_inserted_mid_list_between_fetches() -> None:
    # Lands among entries the scan has ALREADY dated (index 10 < the probes at 15 and 31),
    # on the same day as v009: a tie, so the order stays valid.
    mid = ("mid009", day(9))
    ch = MutatingChannel(numbered(400), {2: lambda c: c.insert(10, mid)})
    ids, _ = ch.check(day(150), day(5), first_batch=16)
    assert "mid009" in ids and ids.index("mid009") == ids.index("v009") + 1


def test_scan_survives_a_shift_that_moves_the_border_itself() -> None:
    # The entry the older border sits on moves down one place when something is published.
    ch = MutatingChannel(
        numbered(300),
        {
            1: lambda c: c.insert(0, ("a", NEWEST + timedelta(days=1))),
            2: lambda c: c.insert(0, ("b", NEWEST + timedelta(days=2))),
        },
    )
    ids, _ = ch.check(day(100), NEWEST + timedelta(days=9), first_batch=8)
    assert ids[:2] == ["b", "a"] and ids[-1] == "v100"


def test_scan_random_mutations_between_fetches_match_a_brute_force_of_the_final_list() -> None:
    rng = random.Random(20260716)
    for _ in range(250):
        n = rng.randint(40, 300)
        items = [(f"v{i:04d}", day(i)) for i in range(n)]
        newest = [0]  # how far into the future the next front insert is dated

        def mutate(
            c: MutatingChannel, rng: random.Random = rng, newest: list[int] = newest
        ) -> None:
            roll = rng.random()
            if roll < 0.4:
                newest[0] += 1
                c.insert(0, (f"n{newest[0]:03d}", NEWEST + timedelta(days=newest[0])))
            elif roll < 0.7 and len(c.items) > 5:
                c.remove(c.items[rng.randrange(len(c.items))][0])
            elif len(c.items) > 5:
                pos = rng.randrange(1, len(c.items))
                c.insert(pos, (f"m{rng.randrange(10**6)}", c.dates[c.items[pos - 1][0]]))

        ch = MutatingChannel(items, {k: mutate for k in range(1, 6)})
        since = NEWEST - timedelta(days=rng.randint(0, n))
        until = NEWEST + timedelta(days=rng.randint(-10, 8))
        if since > until:
            since, until = until, since
        ch.check(since, until, first_batch=rng.randint(2, 24))


# --- a lone out-of-place entry is cheap; a stretch hits the budget -------------------


def test_scan_lone_newer_outlier_costs_a_handful_of_probes_not_a_linear_scan() -> None:
    base = daily(3000)
    since, until = day(2500), NEWEST
    _, clean, _ = FakeChannel(base).scan(since, until)
    dates: list[date | None] = list(base)
    dates[1023] = NEWEST + timedelta(days=3)  # a gallop probe, dated after everything before it
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since, until)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until) and 1023 not in got  # judged by its own date
    # It used to date every entry between the contradicting pair (~500 probes here).
    assert meta["probes"] <= clean["probes"] + 8
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_lone_older_outlier_is_the_one_that_gets_classified() -> None:
    # The OLDER entry of the contradicting pair is the odd one (index 255 dated like 2400,
    # still inside the window), so the far-away newer probe must NOT be the one distrusted.
    base = daily(3000)
    since, until = day(2500), NEWEST
    _, clean, _ = FakeChannel(base).scan(since, until)
    dates: list[date | None] = list(base)
    dates[255] = day(2400)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since, until)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until)
    assert meta["probes"] <= clean["probes"] + 8


def test_scan_two_distant_outliers_stay_separate_and_cheap() -> None:
    # Two inversions far apart used to fuse into ONE hull spanning ~1500 entries.
    base = daily(3000)
    since, until = day(2500), NEWEST
    _, clean, _ = FakeChannel(base).scan(since, until)
    dates: list[date | None] = list(base)
    dates[511] = NEWEST + timedelta(days=4)
    dates[2047] = NEWEST + timedelta(days=5)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since, until)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until) and 511 not in got and 2047 not in got
    assert meta["probes"] <= clean["probes"] + 12
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_a_short_run_of_outliers_is_grown_into_one_island_and_stays_cheap() -> None:
    base = daily(3000)
    since, until = day(2500), NEWEST
    _, clean, _ = FakeChannel(base).scan(since, until)
    dates: list[date | None] = list(base)
    for k in range(1021, 1026):  # five in a row, the gallop probe 1023 among them
        dates[k] = NEWEST + timedelta(days=3 + k)
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since, until)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until)
    assert not {1021, 1022, 1023, 1024, 1025} & set(got)  # incl. 1024/1025, past the first fetch
    assert meta["probes"] <= clean["probes"] + 12


def test_scan_detected_lone_outlier_always_costs_a_constant_number_of_extra_probes() -> None:
    rng = random.Random(11)
    flagged = 0
    for trial in range(1500):
        n = rng.randint(100, 600)
        dates: list[date | None] = []
        d = NEWEST
        for _ in range(n):
            dates.append(d)
            d -= timedelta(days=rng.randint(0, 2))
        since = NEWEST - timedelta(days=rng.randint(0, n))
        until = since + timedelta(days=rng.randint(0, n))
        first_batch = rng.randint(4, 80)
        _, clean, _ = FakeChannel(dates).scan(since, until, first_batch=first_batch)
        dates[rng.randrange(n)] = NEWEST - timedelta(days=rng.randint(-10, 2 * n))
        got, meta, _ = FakeChannel(dates).scan(since, until, first_batch=first_batch)
        if meta["non_monotonic"]:
            flagged += 1
            assert got == brute(dates, since, until), trial
            assert meta["probes"] <= clean["probes"] + 12, (trial, meta, clean)
    assert flagged >= 30  # the property isn't vacuous


# --- ONE out-of-place entry anywhere never loses an in-window video ---------------------
#
# Review finding: a single old entry at the end of a batch stopped the gallop there, and
# nothing was left to contradict it, so every in-window video beyond it silently went
# missing (`--dates` can't recover them: it only re-filters what was returned).


def _monotone(rng: random.Random, n: int) -> list[date | None]:
    """``n`` newest-first dates, 0-2 days apart (ties allowed)."""
    dates: list[date | None] = []
    d = NEWEST
    for _ in range(n):
        dates.append(d)
        d -= timedelta(days=rng.randint(0, 2))
    return dates


def _in_order_truth(dates: list[date | None], k: int, since: date, until: date) -> set[int]:
    """The in-window entries other than the outlier ``k``: what must never go missing."""
    return {i for i, d in enumerate(dates) if i != k and d is not None and since <= d <= until}


def test_scan_old_entry_at_a_batch_boundary_no_longer_hides_the_window() -> None:
    # Repro A from the review: 400 daily videos, entry 63 (the end of the first batch)
    # dated 200 days ago, window = the last 120 days. Used to return 63 of the 120.
    dates: list[date | None] = list(daily(400))
    dates[63] = day(200)
    since, until = day(120), NEWEST
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(since, until)
    assert got == brute(dates, since, until) and len(got) == 120
    assert meta["non_monotonic"] is True  # the outlier was identified, not just survived
    assert meta["flat_requests"] == 2  # 63 alone was not accepted as the end of the window
    assert meta["probes"] <= 16
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_two_outliers_around_a_batch_boundary_lose_no_in_order_entry() -> None:
    # Repro B from the review (two out-of-place entries): 127 dated far too old, 123
    # too new. The in-order in-window videos must all be there; the only extras that may
    # appear are the outliers themselves (judged by position when no probe exposes them).
    dates: list[date | None] = list(daily(400))
    dates[127] = day(380)
    dates[123] = day(60)
    since, until = day(200), day(100)
    got, _, _ = FakeChannel(dates).scan(since, until)
    truth = set(brute(dates, since, until))
    assert truth <= set(got)  # used to return 26 of 99
    assert set(got) - truth <= {123, 127}


@pytest.mark.parametrize("first_batch", [16, 64])
@pytest.mark.parametrize("kind", ["older", "newer"])
def test_scan_outlier_at_every_batch_boundary_loses_nothing(first_batch: int, kind: str) -> None:
    n = 1000
    since, until = day(900), NEWEST
    boundaries = [first_batch * 2**j - 1 for j in range(8) if first_batch * 2**j - 1 < n]
    assert len(boundaries) >= 4
    for k in boundaries:
        dates: list[date | None] = list(daily(n))
        dates[k] = day(k + 1500) if kind == "older" else NEWEST + timedelta(days=500)
        ch = FakeChannel(dates)
        got, meta, _ = ch.scan(since, until, first_batch=first_batch)
        assert _in_order_truth(dates, k, since, until) <= set(got), (k, kind)
        if meta["non_monotonic"]:
            assert got == brute(dates, since, until), (k, kind)
        assert len(ch.probed) == len(set(ch.probed))


def test_scan_gallop_needs_two_old_entries_in_a_row_before_it_stops() -> None:
    dates = daily(1000)
    # The window ends in the middle of the second batch: 127 and 126 are both old.
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(100), NEWEST)
    assert got == list(range(101)) and meta["flat_requests"] == 2
    # The window ends exactly where the first batch does: 63 is the FIRST old entry,
    # its predecessor is in the window, so one old date is not accepted as the end —
    # one more batch is fetched (a rare, cheap miss) and the answer is the same.
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(62), NEWEST)
    assert got == list(range(63)) and meta["flat_requests"] == 2
    # Both old inside the first batch: it stops there.
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(30), NEWEST)
    assert got == list(range(31)) and meta["flat_requests"] == 1


def test_scan_gallop_stop_is_proved_again_when_the_entries_it_rested_on_are_distrusted() -> None:
    dates = daily(1000)
    ch = FakeChannel(dates)
    scan = ytdl._WindowScan(
        ch.fetch_flat,
        ch.probe_date,
        day(100),
        NEWEST,
        cap=5000,
        first_batch=64,
        max_probes=200,
        max_undatable_run=24,
        skip=None,
    )
    assert scan.gallop() is True and ch.requests == [64, 128]  # stopped: 127 and 126 are old
    assert scan.gallop() is False and ch.requests == [64, 128]  # nothing changed: free re-check
    # Everything old that the stop could lean on becomes bad (as `_resolve` would do):
    scan.bad.update(scan.ids[101:])
    assert scan.gallop() is True  # the stop no longer holds: it fetches more
    assert ch.requests[2] == 256


def test_scan_run_resumes_the_gallop_once_the_search_has_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # White-box: no natural input I could build (6000 random multi-outlier trials found
    # none) reaches this, so the first `bounds()` is made to distrust the old tail the way
    # `_resolve` would. `run` must then notice that its stop is gone and fetch more.
    dates = daily(1000)
    ch = FakeChannel(dates)
    real_bounds = ytdl._WindowScan.bounds
    calls: list[int] = []

    def bounds(self: ytdl._WindowScan) -> tuple[int, int]:
        if not calls:
            calls.append(1)
            for i in range(101, len(self.ids)):  # every old entry the stop leaned on
                self.date_at(i, check=False)  # ... dated, as `_resolve`'s bad entries always are
            self.bad.update(self.ids[101:])
        return real_bounds(self)

    monkeypatch.setattr(ytdl._WindowScan, "bounds", bounds)
    got, meta, _ = ch.scan(day(100), NEWEST)
    assert ch.requests == [64, 128, 256]  # without the resume it would stop at [64, 128]
    assert got == list(range(101))  # the distrusted entries are judged by their own date
    assert meta["truncated_by_cap"] is False


def test_scan_gallop_clears_truncated_when_a_later_check_proves_the_stop() -> None:
    dates = daily(1000)
    ch = FakeChannel(dates)
    scan = ytdl._WindowScan(
        ch.fetch_flat,
        ch.probe_date,
        day(100),
        NEWEST,
        cap=64,
        first_batch=64,
        max_probes=200,
        max_undatable_run=24,
        skip=None,
    )
    scan.gallop()
    assert scan.truncated is True  # at the cap with the window still open
    scan.since = day(10)  # (white-box) the window now ends inside the list
    assert scan.gallop() is False and scan.truncated is False  # no stale warning


def test_scan_a_lone_outlier_is_distrusted_alone() -> None:
    # Its neighbours vouch for the far-away entry it contradicts, so only the outlier —
    # not the legitimate probe on the other side of the contradiction — loses its place.
    dates: list[date | None] = list(daily(400))
    dates[63] = day(200)
    ch = FakeChannel(dates)
    scan = ytdl._WindowScan(
        ch.fetch_flat,
        ch.probe_date,
        day(120),
        NEWEST,
        cap=5000,
        first_batch=64,
        max_probes=200,
        max_undatable_run=24,
        skip=None,
    )
    scan.run()
    assert scan.bad == {"v63"}


def test_scan_single_outlier_anywhere_never_loses_an_in_window_entry() -> None:
    # Property, 1000 seeded trials over channels of 50..3000 entries, ONE outlier (newer or
    # older, at a random place, at a batch boundary, or next to a border of the window):
    #  * nothing in-order that is inside the window may be missing from the result;
    #  * once the scan flags the inversion, the result equals the filter by actual date;
    #  * a lone outlier costs only a few extra probes over the same scan of the clean channel.
    rng = random.Random(20260717)
    flagged = boundary = border = costly = 0
    for trial in range(1000):
        n = rng.randint(50, 3000)
        dates = _monotone(rng, n)
        oldest = dates[-1]
        assert oldest is not None
        span = (NEWEST - oldest).days
        since = NEWEST - timedelta(days=rng.randint(0, span))
        until = since + timedelta(days=rng.randint(0, span))
        first_batch = rng.choice([16, 32, 64, 64, 100])
        _, clean, _ = FakeChannel(dates).scan(since, until, first_batch=first_batch)
        truth = brute(dates, since, until)
        mode = rng.choice(["random", "boundary", "border", "random"])
        if mode == "boundary":
            ends = [first_batch * 2**j - 1 for j in range(8) if first_batch * 2**j - 1 < n]
            k = rng.choice(ends or [n - 1])
            boundary += 1
        elif mode == "border" and truth:
            k = max(0, min(n - 1, rng.choice([truth[0] - 1, truth[0], truth[-1], truth[-1] + 1])))
            border += 1
        else:
            k = rng.randrange(n)
        if rng.random() < 0.3:  # a date from inside the channel's own span, in or out of the window
            outlier = NEWEST - timedelta(days=rng.randint(-30, span + 30))
        elif rng.random() < 0.5:
            outlier = NEWEST + timedelta(days=rng.randint(1, 400))  # far too new
        else:
            outlier = oldest - timedelta(days=rng.randint(1, 400))  # far too old
        bad = list(dates)
        bad[k] = outlier
        ch = FakeChannel(bad)
        got, meta, _ = ch.scan(since, until, first_batch=first_batch)
        where = (trial, n, k, mode, first_batch, since, until, outlier)
        assert _in_order_truth(bad, k, since, until) <= set(got), where
        if meta["non_monotonic"]:
            flagged += 1
            assert got == brute(bad, since, until), where
        extra = meta["probes"] - clean["probes"]
        assert extra <= 12, (extra, where)
        costly += extra > 3
        assert len(ch.probed) == len(set(ch.probed)), where
    assert flagged >= 200 and boundary >= 200 and border >= 100  # the property isn't vacuous
    # 3 extra probes or fewer in 97%+ of the trials (measured ~98.8%). Dating the
    # neighbours of a contradicting probe is what keeps this low: without it the
    # share above 3 is ~10%.
    assert costly <= 30


def test_scan_single_outlier_family_on_a_3000_entry_channel_stays_cheap_and_complete() -> None:
    # Every batch boundary and both borders of five windows, four kinds of wrong date.
    # Cost lock: a lone outlier adds few probes (<= 4 in 95% of the cases, never > 12).
    base = daily(3000)
    windows = [
        (day(2500), NEWEST),
        (day(1500), day(700)),
        (day(300), day(100)),
        (day(2999), day(0)),
        (day(1000), day(1000)),
    ]
    extras: list[int] = []
    for since, until in windows:
        _, clean, _ = FakeChannel(base).scan(since, until)
        inside = [i for i, d in enumerate(base) if since <= d <= until]
        hi_t, lo_t = inside[0], inside[-1] + 1
        spots = {63, 127, 255, 511, 1023, 2047, 0, 1, 2999}
        spots |= {lo_t - 2, lo_t - 1, lo_t, lo_t + 1, hi_t - 2, hi_t - 1, hi_t, hi_t + 1}
        for k in sorted(s for s in spots if 0 <= s < 3000):
            for outlier in (
                NEWEST + timedelta(days=400),  # far newer than anything
                day(3400),  # far older than anything
                day(max(0, k - 400)),  # newer than its place, within the channel's span
                day(k + 400),  # older than its place, within the channel's span
            ):
                dates: list[date | None] = list(base)
                dates[k] = outlier
                got, meta, _ = FakeChannel(dates).scan(since, until)
                assert _in_order_truth(dates, k, since, until) <= set(got), (since, until, k)
                if meta["non_monotonic"]:
                    assert got == brute(dates, since, until), (since, until, k, outlier)
                extras.append(meta["probes"] - clean["probes"])
    assert len(extras) >= 250
    assert max(extras) <= 12
    assert sum(e > 4 for e in extras) <= 0.05 * len(extras)


def test_scan_clean_3000_entry_channel_stays_within_30_probes_for_every_kind_of_window() -> None:
    # Includes `until = today`-style windows (the CLI default), where the border
    # confirmations are the new cost.
    dates = daily(3000)
    rng = random.Random(3)
    windows = []
    for _ in range(150):
        a, b = sorted(rng.sample(range(3000), 2))
        windows.append((day(b), day(a)))
        windows.append((day(b), NEWEST + timedelta(days=rng.randint(0, 5))))
    worst = 0
    for since, until in windows:
        got, meta, _ = FakeChannel(dates).scan(since, until)
        assert got == brute(dates, since, until)
        worst = max(worst, meta["probes"])
    assert worst <= 30


def _two_sections() -> list[date | None]:
    """1500 OLD videos listed first, then 1500 newer ones: a long stretch out of order."""
    return [day(i + 1500) for i in range(1500)] + [day(i) for i in range(1500)]


def test_scan_a_long_stretch_out_of_order_exhausts_the_probe_budget() -> None:
    dates = _two_sections()
    ch = FakeChannel(dates)
    with pytest.raises(ytdl.WindowScanError, match=r"200 date probes"):
        ch.scan(day(2999), NEWEST)
    assert len(ch.probed) == 200  # the budget is a hard ceiling, never overshot
    assert issubclass(ytdl.WindowScanError, RuntimeError)

    ch = FakeChannel(dates)
    with pytest.raises(ytdl.WindowScanError, match=r"40 date probes"):
        ch.scan(day(2999), NEWEST, max_probes=40)
    assert len(ch.probed) == 40


def test_scan_probe_budget_zero_means_unlimited_and_still_gets_it_right() -> None:
    dates = _two_sections()
    ch = FakeChannel(dates)
    got, meta, _ = ch.scan(day(2999), NEWEST, max_probes=0)
    assert got == brute(dates, day(2999), NEWEST)
    assert meta["non_monotonic"] is True and meta["probes"] > 1000  # one by one, as it says


def test_scan_probe_budget_applies_to_an_ordinary_channel_too() -> None:
    ch = FakeChannel(daily(500))
    with pytest.raises(ytdl.WindowScanError):
        ch.scan(day(300), day(100), max_probes=3)
    assert len(ch.probed) == 3
    # ... and a budget the search fits in changes nothing.
    got, _, _ = FakeChannel(daily(500)).scan(day(300), day(100), max_probes=30)
    assert got == list(range(100, 301))


def test_scan_rejects_negative_guard_limits() -> None:
    ch = FakeChannel(daily(10))
    with pytest.raises(ValueError, match="max_probes"):
        ch.scan(day(5), day(1), max_probes=-1)
    with pytest.raises(ValueError, match="max_undatable_run"):
        ch.scan(day(5), day(1), max_undatable_run=-1)


def test_out_of_order_helper_returns_the_entries_no_best_run_can_do_without() -> None:
    d = [day(i) for i in range(10)]
    assert ytdl._out_of_order(d) == set()  # ordered: nothing to distrust
    assert ytdl._out_of_order([]) == set()
    lone = list(d)
    lone[5] = NEWEST + timedelta(days=3)
    assert ytdl._out_of_order(lone) == {5}  # evidence is unambiguous: just that one
    swapped = list(d)
    swapped[4], swapped[5] = swapped[5], swapped[4]
    assert ytdl._out_of_order(swapped) == {4, 5}  # a tie: both, never a guess
    assert ytdl._out_of_order([day(1), day(1), day(1)]) == set()  # ties in dates are fine


# ---------------------------------------------------------------------------
# yt-dlp wrappers, through a fake of ytdl._extract
# ---------------------------------------------------------------------------


class FakeYt:
    """Stand-in for ``ytdl._extract``: a ``/videos`` tab that honours ``playlistend``
    and watch pages that carry ``upload_date``. Records every call."""

    def __init__(
        self,
        uploads: list[tuple[str, date | None]],
        *,
        name: str = "Fake Channel",
        broken: set[str] | None = None,
        errors: dict[str, Exception] | None = None,
        entry_fields: dict[str, dict] | None = None,
        watch_fields: dict[str, dict] | None = None,
        fail_pattern: Callable[[int], Exception | None] | None = None,
    ) -> None:
        self.uploads = uploads
        self.name = name
        self.broken = broken or set()
        self.errors = errors or {}  # vid -> the exception its watch page raises
        self.entry_fields = entry_fields or {}  # vid -> extra keys on its FLAT entry
        self.watch_fields = watch_fields or {}  # vid -> extra keys on its watch-page info
        self.fail_pattern = fail_pattern  # watch-call number (0-based) -> exception or None
        self.calls: list[tuple[str, dict]] = []

    @property
    def tab_calls(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if c[0].endswith("/videos")]

    @property
    def watch_calls(self) -> list[str]:
        return [c[0].split("v=")[1] for c in self.calls if "watch?v=" in c[0]]

    def __call__(self, url: str, opts: dict) -> dict:
        self.calls.append((url, dict(opts)))
        if url.endswith("/videos"):
            n = opts["playlistend"]
            entries = [
                {
                    "id": vid,
                    "title": f"title {vid}",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "view_count": 100 + i,
                    **self.entry_fields.get(vid, {}),
                }
                for i, (vid, _) in enumerate(self.uploads[:n])
            ]
            return {"channel": self.name, "entries": entries}
        vid = url.split("v=")[1]
        n_watch = len(self.watch_calls) - 1  # this call is already recorded
        if self.fail_pattern is not None and (exc := self.fail_pattern(n_watch)) is not None:
            raise exc
        if vid in self.errors:
            raise self.errors[vid]
        if vid in self.broken:
            raise RuntimeError(f"extraction failed for {vid}")
        d = dict(self.uploads)[vid]
        return {
            "upload_date": d.strftime("%Y%m%d") if d else None,
            **self.watch_fields.get(vid, {}),
        }


def make_uploads(n: int) -> list[tuple[str, date | None]]:
    return [(f"vid{i:03d}", day(i)) for i in range(n)]


@pytest.fixture
def fake_yt(monkeypatch: pytest.MonkeyPatch):
    def install(yt: FakeYt) -> FakeYt:
        monkeypatch.setattr(ytdl, "_extract", yt)
        return yt

    return install


# --- video_upload_date ---------------------------------------------------------


def test_video_upload_date_parses_yyyymmdd(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict]] = []

    def fake(url: str, opts: dict) -> dict:
        seen.append((url, opts))
        return {"upload_date": "20260503", "title": "x"}

    monkeypatch.setattr(ytdl, "_extract", fake)
    assert video_upload_date("abcdefghijk") == date(2026, 5, 3)
    # REWRITTEN (was `{}`): a metadata-only lookup asks yt-dlp not to fail on formats,
    # which is what made members-only/age-gated/premiere videos raise.
    assert seen == [
        ("https://www.youtube.com/watch?v=abcdefghijk", {"ignore_no_formats_error": True})
    ]


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"upload_date": None},
        {"upload_date": ""},
        {"upload_date": "2026050"},  # too short
        {"upload_date": "202605033"},  # too long
        {"upload_date": "2026-05-03"},
        {"upload_date": "abcdefgh"},
        {"upload_date": "20261340"},  # no such month/day
        {"upload_date": "20260230"},  # no such day
        {"upload_date": "٢٠٢٦٠٥٠٣"},  # non-ASCII digits
    ],
)
def test_video_upload_date_is_none_when_missing_or_unparseable(
    monkeypatch: pytest.MonkeyPatch, info: dict
) -> None:
    monkeypatch.setattr(ytdl, "_extract", lambda url, opts: info)
    assert video_upload_date("abcdefghijk") is None


def test_video_upload_date_accepts_an_int_upload_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ytdl, "_extract", lambda url, opts: {"upload_date": 20260503})
    assert video_upload_date("abcdefghijk") == date(2026, 5, 3)


def test_video_upload_date_lets_exceptions_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, opts: dict) -> dict:
        raise RuntimeError("private video")

    monkeypatch.setattr(ytdl, "_extract", boom)
    with pytest.raises(RuntimeError, match="private video"):
        video_upload_date("abcdefghijk")


# --- channel_entries_window ------------------------------------------------------


def test_channel_entries_window_recent(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(200)))
    name, items, meta = channel_entries_window("@chan", day(30), day(10))
    assert name == "Fake Channel"
    assert [it["video_id"] for it in items] == [
        f"vid{i:03d}" for i in range(10, 31)
    ]  # newest first
    first = items[0]
    assert set(first) - {"date"} == {"video_id", "title", "views", "views_int", "url"}
    assert first["title"] == "title vid010" and first["views_int"] == 110
    assert first["url"] == "https://www.youtube.com/watch?v=vid010"
    assert all("_date" not in it for it in items)  # the search's bookkeeping never leaks
    assert meta["scanned"] == 64 and meta["flat_requests"] == 1
    assert meta["truncated_by_cap"] is False and meta["non_monotonic"] is False
    # The tab was asked flat, with playlistend = the gallop's first batch.
    url, opts = yt.tab_calls[0]
    assert url == "https://www.youtube.com/@chan/videos"
    assert opts == {"extract_flat": "in_playlist", "playlistend": 64}
    # Every probe is one watch-page extraction (metadata only: formats may be missing),
    # and none repeats. REWRITTEN: the options used to be `{}`.
    assert len(yt.watch_calls) == len(set(yt.watch_calls)) == meta["probes"]
    assert all(c[1] == {"ignore_no_formats_error": True} for c in yt.calls if "watch?v=" in c[0])


def test_channel_entries_window_dates_only_the_probed_items(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(200)))
    _, items, _ = channel_entries_window("@chan", day(150), day(10))
    dated = [it for it in items if "date" in it]
    assert dated and len(dated) < len(items)
    by_id = dict(make_uploads(200))
    for it in dated:
        assert it["date"] == by_id[it["video_id"]].isoformat()
        assert it["video_id"] in yt.watch_calls  # a date is only ever attached if it was paid for


def test_channel_entries_window_old_is_the_same_window_reversed(fake_yt) -> None:
    fake_yt(FakeYt(make_uploads(200)))
    _, recent, _ = channel_entries_window("@chan", day(30), day(10))
    _, old, _ = channel_entries_window("@chan", day(30), day(10), order="old")
    assert [it["video_id"] for it in old] == [it["video_id"] for it in reversed(recent)]
    assert old[0]["video_id"] == "vid030" and old[-1]["video_id"] == "vid010"


def test_channel_entries_window_popular_is_not_implemented_before_any_io(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(50)))
    with pytest.raises(NotImplementedError, match="popular"):
        channel_entries_window("@chan", day(30), day(10), order="popular")
    assert yt.calls == []


@pytest.mark.parametrize("order", ["oldest", "", "RECENT", "newest"])
def test_channel_entries_window_unknown_order_is_a_value_error(fake_yt, order: str) -> None:
    yt = fake_yt(FakeYt(make_uploads(50)))
    with pytest.raises(ValueError, match="unknown order"):
        channel_entries_window("@chan", day(30), day(10), order=order)
    assert yt.calls == []


def test_channel_entries_window_empty_tab_reports_scanned_zero(fake_yt) -> None:
    fake_yt(FakeYt([], name="Empty Co"))
    name, items, meta = channel_entries_window("@chan", day(30), day(10))
    assert (name, items) == ("Empty Co", [])
    assert meta["scanned"] == 0  # the caller's cue to fall back, vs scanned > 0 == empty window


def test_channel_entries_window_empty_window_is_distinguishable_from_an_empty_tab(fake_yt) -> None:
    fake_yt(FakeYt(make_uploads(100)))
    _, items, meta = channel_entries_window("@chan", date(2000, 1, 1), date(2000, 2, 1))
    assert items == []
    assert meta["scanned"] == 100


def test_channel_entries_window_since_after_until_is_a_value_error_before_any_io(fake_yt) -> None:
    # REWRITTEN (was: returns ("", [], meta) with scanned == 0 and no I/O). `scanned == 0`
    # is this function's "the tab was empty, fall back" signal, so a caller bug must not
    # be able to produce it: an inverted range is refused instead.
    yt = fake_yt(FakeYt(make_uploads(50)))
    with pytest.raises(ValueError, match="after until"):
        channel_entries_window("@chan", day(10), day(30))
    assert yt.calls == []
    # An order error still comes first, and both are raised before any network call.
    with pytest.raises(ValueError, match="unknown order"):
        channel_entries_window("@chan", day(10), day(30), order="nope")
    # A one-day window (since == until) is fine, and the pure scan still reads an
    # inverted range as an empty window.
    _, items, _ = channel_entries_window("@chan", day(10), day(10))
    assert [it["video_id"] for it in items] == ["vid010"]
    assert scan_window(lambda n: [], lambda e: None, day(10), day(30)) == (
        [],
        {
            "probes": 0,
            "flat_requests": 0,
            "scanned": 0,
            "truncated_by_cap": False,
            "non_monotonic": False,
        },
    )


def test_channel_entries_window_name_falls_back_to_uploader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ytdl, "_extract", lambda url, opts: {"uploader": "Up Loader", "entries": []}
    )
    name, items, _ = channel_entries_window("@chan", day(30), day(10))
    assert name == "Up Loader" and items == []


def test_channel_entries_window_honours_the_cap(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(300)))
    _, items, meta = channel_entries_window("@chan", day(299), day(0), cap=100)
    assert meta["truncated_by_cap"] is True
    assert len(items) == 100
    assert [c[1]["playlistend"] for c in yt.tab_calls] == [64, 100]


def test_channel_entries_window_keeps_entries_without_an_id_undated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An entry with no id has nothing to probe: it's undatable (kept, per the
    # conservative rule) and must not trigger a watch-page request for "".
    calls: list[str] = []

    def fake(url: str, opts: dict) -> dict:
        calls.append(url)
        return {"channel": "C", "entries": [{"title": f"t{i}"} for i in range(3)]}

    monkeypatch.setattr(ytdl, "_extract", fake)
    _, items, meta = channel_entries_window("@chan", day(30), day(10))
    assert [it["title"] for it in items] == ["t0", "t1", "t2"]
    assert all(it["video_id"] == "" and "date" not in it for it in items)
    # Only the tab was fetched: `probes` counts probe_date calls, none of which hit the network.
    assert calls == ["https://www.youtube.com/@chan/videos"]
    assert meta["scanned"] == 3


def test_channel_entries_window_propagates_a_failing_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, opts: dict) -> dict:
        raise RuntimeError("HTTP Error 429")

    monkeypatch.setattr(ytdl, "_extract", boom)
    with pytest.raises(RuntimeError, match="429"):
        channel_entries_window("@chan", day(30), day(10))


def test_channel_entries_window_propagates_a_failing_probe(fake_yt) -> None:
    # Swallowing it would read a blanket outage as "every entry is undatable"
    # and silently return the whole scan.
    uploads = make_uploads(100)
    fake_yt(FakeYt(uploads, broken={vid for vid, _ in uploads}))
    with pytest.raises(RuntimeError, match="extraction failed"):
        channel_entries_window("@chan", day(60), day(10))


# --- fill_dates --------------------------------------------------------------------


def test_fill_dates_dates_only_the_missing_ones(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(10)))
    items = [
        {"video_id": "vid001", "title": "a", "date": "2000-01-01"},  # already dated: untouched
        {"video_id": "vid002", "title": "b"},
        {"video_id": "vid003", "title": "c", "date": ""},  # empty counts as missing
        {"video_id": "vid004", "title": "d", "date": None},
    ]
    assert fill_dates(items) == 3
    assert [it.get("date") for it in items] == [
        "2000-01-01",
        day(2).isoformat(),
        day(3).isoformat(),
        day(4).isoformat(),
    ]
    assert sorted(yt.watch_calls) == ["vid002", "vid003", "vid004"]  # vid001 never re-fetched


def test_fill_dates_tolerates_a_failing_video(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(10), broken={"vid003"}))
    items = [{"video_id": f"vid{i:03d}"} for i in range(1, 6)]
    assert fill_dates(items) == 4  # the one that raised is simply not counted
    assert "date" not in items[2]
    assert [it["date"] for it in items if "date" in it] == [
        day(i).isoformat() for i in (1, 2, 4, 5)
    ]
    assert len(yt.watch_calls) == 5


def test_fill_dates_leaves_undatable_videos_undated(fake_yt) -> None:
    uploads: list[tuple[str, date | None]] = [("vidaaa", None), ("vidbbb", day(5))]
    fake_yt(FakeYt(uploads))
    items = [{"video_id": "vidaaa"}, {"video_id": "vidbbb"}]
    assert fill_dates(items) == 1
    assert "date" not in items[0] and items[1]["date"] == day(5).isoformat()


def test_fill_dates_keeps_order_and_identity(fake_yt) -> None:
    fake_yt(FakeYt(make_uploads(40)))
    items = [{"video_id": f"vid{i:03d}", "n": i} for i in reversed(range(40))]
    snapshot = list(items)
    fill_dates(items, workers=4)
    assert items == snapshot and all(a is b for a, b in zip(items, snapshot, strict=True))
    assert [it["n"] for it in items] == list(reversed(range(40)))
    assert all(it["date"] == day(it["n"]).isoformat() for it in items)


def test_fill_dates_with_nothing_to_do_makes_no_requests(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(5)))
    assert fill_dates([]) == 0
    assert fill_dates([{"video_id": "vid001", "date": "2026-01-01"}]) == 0
    assert fill_dates([{"title": "no id"}, {"video_id": ""}]) == 0  # nothing to look up
    assert yt.calls == []


def test_fill_dates_runs_in_parallel_but_never_above_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(url: str, opts: dict) -> dict:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return {"upload_date": "20260503"}

    monkeypatch.setattr(ytdl, "_extract", slow)
    items = [{"video_id": f"v{i}"} for i in range(8)]
    assert fill_dates(items, workers=2) == 8
    assert state["peak"] == 2

    state.update(now=0, peak=0)
    items = [{"video_id": f"v{i}"} for i in range(8)]
    assert fill_dates(items, workers=1) == 8
    assert state["peak"] == 1

    state.update(now=0, peak=0)
    items = [{"video_id": f"v{i}"} for i in range(3)]
    assert fill_dates(items, workers=0) == 3  # nonsense worker count degrades to 1, not a crash
    assert state["peak"] == 1


def test_fill_dates_default_ceiling_is_four(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow(url: str, opts: dict) -> dict:
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return {"upload_date": "20260503"}

    monkeypatch.setattr(ytdl, "_extract", slow)
    items = [{"video_id": f"v{i}"} for i in range(16)]
    assert fill_dates(items) == 16
    assert state["peak"] == 4


# --- unreadable / unpublished videos must not sink the scan ---------------------------


def per_video_error(msg: str = "This video is private") -> Exception:
    """What yt-dlp raises for a verdict about ONE video: a DownloadError wrapping an
    ``expected`` ExtractorError with no cause (shape observed on yt-dlp 2026.07)."""
    from yt_dlp.utils import DownloadError, ExtractorError

    return DownloadError(
        f"ERROR: {msg}", (ExtractorError, ExtractorError(msg, expected=True), None)
    )


def network_error() -> Exception:
    """A refused connection as yt-dlp reports it: a DownloadError wrapping a
    ``yt_dlp.networking`` exception, which is not an ExtractorError at all."""
    from yt_dlp.networking.exceptions import TransportError
    from yt_dlp.utils import DownloadError

    inner = TransportError("Unable to connect to proxy")
    return DownloadError("ERROR: Unable to download API page", (TransportError, inner, None))


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (per_video_error, True),
        (lambda: per_video_error("Sign in to confirm your age"), True),
        (network_error, False),
        (lambda: RuntimeError("boom"), False),
        (lambda: ValueError("not yt-dlp at all"), False),
    ],
)
def test_per_video_error_classification(make, expected: bool) -> None:
    assert ytdl._is_per_video_error(make()) is expected


def test_per_video_error_needs_expected_and_no_network_cause() -> None:
    from yt_dlp.networking.exceptions import TransportError
    from yt_dlp.utils import DownloadError, ExtractorError

    bare = ExtractorError("This video is unavailable", expected=True)
    assert ytdl._is_per_video_error(bare) is True  # unwrapped works too
    unexpected = ExtractorError("something odd")
    assert ytdl._is_per_video_error(unexpected) is False
    http = ExtractorError("HTTP Error 429", expected=True, cause=TransportError("x"))
    assert ytdl._is_per_video_error(http) is False  # a network cause says nothing about THIS video
    wrapped = DownloadError("ERROR: HTTP Error 429", (ExtractorError, http, None))
    assert ytdl._is_per_video_error(wrapped) is False
    assert ytdl._is_per_video_error(DownloadError("no exc_info at all")) is False


def test_video_upload_date_of_an_upcoming_video_is_none_even_with_an_upload_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Observed live: an upcoming video's `upload_date` is the day the EVENT PAGE was
    # created (days before the scheduled start), not a publication.
    info = {
        "upload_date": "20261111",
        "live_status": "is_upcoming",
        "release_timestamp": 1794680100,
    }
    monkeypatch.setattr(ytdl, "_extract", lambda url, opts: info)
    assert video_upload_date("abcdefghijk") is None


@pytest.mark.parametrize(
    "extra",
    [
        {"live_status": "not_live", "availability": "subscriber_only"},  # members-only
        {"live_status": "not_live", "availability": "needs_auth"},  # age-gated
        {"live_status": "is_live"},  # live now: dated by when it started
        {"live_status": "was_live"},
        {"live_status": None},
    ],
)
def test_video_upload_date_keeps_the_date_of_every_other_kind_of_video(
    monkeypatch: pytest.MonkeyPatch, extra: dict
) -> None:
    monkeypatch.setattr(ytdl, "_extract", lambda url, opts: {"upload_date": "20261104", **extra})
    assert video_upload_date("abcdefghijk") == date(2026, 11, 4)


def test_an_unreadable_video_at_index_zero_does_not_sink_the_default_until_today_scan(
    fake_yt,
) -> None:
    # `until = today` always probes the newest entry first — exactly where a premiere or
    # an unreadable video sits. It is undatable (kept inside), and the scan carries on.
    uploads = make_uploads(200)
    fake_yt(FakeYt(uploads, errors={"vid000": per_video_error("This video is private")}))
    _, items, meta = channel_entries_window("@chan", day(30), NEWEST + timedelta(days=30))
    assert [it["video_id"] for it in items] == [f"vid{i:03d}" for i in range(0, 31)]
    assert "date" not in items[0]  # nothing to date it by
    assert meta["scanned"] == 64


def test_unreadable_videos_in_the_middle_are_undatable_not_fatal(fake_yt) -> None:
    uploads = make_uploads(300)
    bad = {f"vid{i:03d}": per_video_error() for i in (63, 62, 127, 100, 101)}
    fake_yt(FakeYt(uploads, errors=bad))
    _, items, _ = channel_entries_window("@chan", day(200), day(10))
    assert [it["video_id"] for it in items] == [f"vid{i:03d}" for i in range(10, 201)]


def test_upcoming_entries_are_skipped_from_the_flat_data_and_never_probed(fake_yt) -> None:
    uploads = make_uploads(200)
    upcoming = {"live_status": "is_upcoming"}
    yt = fake_yt(
        FakeYt(
            uploads,
            entry_fields={"vid000": upcoming, "vid001": upcoming},
            # What the watch page would say if it WERE probed: a creation date, not a release.
            watch_fields={"vid000": {"live_status": "is_upcoming"}},
        )
    )
    _, items, meta = channel_entries_window("@chan", day(30), NEWEST + timedelta(days=30))
    assert [it["video_id"] for it in items] == [f"vid{i:03d}" for i in range(2, 31)]
    assert "vid000" not in yt.watch_calls and "vid001" not in yt.watch_calls
    assert meta["scanned"] == 64  # the raw flat count, upcoming entries included


def test_dropping_upcoming_entries_never_makes_a_full_reply_look_exhausted(fake_yt) -> None:
    # 6 upcoming at the front: the first 64-entry reply keeps only 58. That must NOT read
    # as "the channel ended" — the 6 oldest videos (vid064..vid069) are still to come.
    uploads = make_uploads(70)
    upcoming = {"live_status": "is_upcoming"}
    fields = {f"vid{i:03d}": upcoming for i in range(6)}
    yt = fake_yt(FakeYt(uploads, entry_fields=fields))
    _, items, meta = channel_entries_window("@chan", day(69), NEWEST + timedelta(days=30))
    assert [it["video_id"] for it in items] == [f"vid{i:03d}" for i in range(6, 70)]
    assert [c[1]["playlistend"] for c in yt.tab_calls] == [64, 128]
    assert meta["scanned"] == 70


def test_window_made_only_of_upcoming_entries_is_empty_but_scanned(fake_yt) -> None:
    uploads = make_uploads(3)
    fields = {vid: {"live_status": "is_upcoming"} for vid, _ in uploads}
    fake_yt(FakeYt(uploads, entry_fields=fields))
    _, items, meta = channel_entries_window("@chan", day(30), NEWEST)
    assert items == [] and meta["scanned"] == 3  # an answered, empty window — not a failed tab


def test_three_blanket_failures_in_a_row_re_raise_the_last_error(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(200), fail_pattern=lambda n: RuntimeError(f"net down #{n}")))
    with pytest.raises(RuntimeError, match=r"net down #2"):
        channel_entries_window("@chan", day(60), day(10))
    assert len(yt.watch_calls) == 3  # the breaker trips on the third, not later


def test_a_network_error_counts_toward_the_breaker_too(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(200), fail_pattern=lambda n: network_error()))
    with pytest.raises(Exception, match="Unable to download API page"):
        channel_entries_window("@chan", day(60), day(10))
    assert len(yt.watch_calls) == 3


def test_two_blanket_failures_then_a_success_do_not_trip_the_breaker(fake_yt) -> None:
    # fail, fail, ok, fail, fail, ok, ...: never three in a row.
    yt = fake_yt(
        FakeYt(
            make_uploads(300),
            fail_pattern=lambda n: RuntimeError("blip") if n % 3 != 2 else None,
        )
    )
    _, items, meta = channel_entries_window("@chan", day(150), day(10))
    assert items and meta["probes"] >= 6
    assert len(yt.watch_calls) >= 6


def test_a_per_video_verdict_resets_the_blanket_failure_count(fake_yt) -> None:
    # net, net, VIDEO, net, net, ok... YouTube answered at call 2, so the net failures
    # before and after it are separate blips, not three in a row.
    script = {
        0: RuntimeError("blip"),
        1: RuntimeError("blip"),
        2: per_video_error(),
        3: RuntimeError("blip"),
        4: RuntimeError("blip"),
    }
    yt = fake_yt(FakeYt(make_uploads(300), fail_pattern=lambda n: script.get(n)))
    channel_entries_window("@chan", day(150), day(10))  # must not raise
    assert len(yt.watch_calls) >= 6


def test_every_probe_refused_as_per_video_ends_in_the_streak_guard(fake_yt) -> None:
    # A bot wall looks per-video (expected ExtractorError); the undatable streak catches it.
    yt = fake_yt(
        FakeYt(make_uploads(300), fail_pattern=lambda n: per_video_error("Sign in to confirm"))
    )
    with pytest.raises(ytdl.WindowScanError, match="in a row"):
        channel_entries_window("@chan", day(150), day(10))
    assert len(yt.watch_calls) == 25


def test_window_scan_error_is_what_the_cli_sees_as_an_engine_failure(fake_yt) -> None:
    # cli.run_channel_batch catches Exception around channel_entries_window and falls
    # back to Selenium; the dedicated error must be one.
    fake_yt(FakeYt(make_uploads(300), fail_pattern=lambda n: per_video_error()))
    with pytest.raises(Exception) as excinfo:
        channel_entries_window("@chan", day(150), day(10))
    assert isinstance(excinfo.value, ytdl.WindowScanError)


# --- fill_dates: a total outage is no longer silent ----------------------------------


def _viewlyt_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "viewlyt" and r.levelno == logging.WARNING]


def test_fill_dates_warns_once_with_the_count_when_everything_fails(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    # REWRITTEN: the failures used to be plain RuntimeErrors. Those are now network-level
    # (unrecognised) and trip the new breaker after 3, so "every request failed" is asserted
    # with per-video verdicts here (a removed/private video never trips it); the outage
    # case has its own tests below.
    uploads = make_uploads(10)
    errors = {vid: per_video_error("This video is unavailable") for vid, _ in uploads}
    fake_yt(FakeYt(uploads, errors=errors))
    items = [{"video_id": f"vid{i:03d}"} for i in range(5)]
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(items) == 0
    (rec,) = _viewlyt_warnings(caplog)  # exactly one record, not one per video
    msg = rec.getMessage()
    assert "5 of 5" in msg and "5 request(s) failed" in msg and "unavailable" in msg
    assert "not attempted" not in msg  # per-video verdicts never stop the batch


def test_fill_dates_warning_counts_only_what_stayed_undated(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    fake_yt(FakeYt(make_uploads(10), broken={"vid003"}))
    items = [{"video_id": f"vid{i:03d}"} for i in range(1, 6)] + [
        {"video_id": "vid009", "date": "2026-01-01"}  # already dated: not part of the count
    ]
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(items) == 4
    (rec,) = _viewlyt_warnings(caplog)
    assert "1 of 5" in rec.getMessage() and "1 request(s) failed" in rec.getMessage()


def test_fill_dates_warning_for_videos_that_simply_have_no_date(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    fake_yt(FakeYt([("vidaaa", None), ("vidbbb", day(3))]))
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates([{"video_id": "vidaaa"}, {"video_id": "vidbbb"}]) == 1
    (rec,) = _viewlyt_warnings(caplog)
    assert "1 of 2" in rec.getMessage() and "no publish date" in rec.getMessage()


def test_fill_dates_is_silent_when_everything_was_dated_or_nothing_to_do(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    fake_yt(FakeYt(make_uploads(5)))
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates([{"video_id": "vid001"}, {"video_id": "vid002"}]) == 2
        assert fill_dates([]) == 0
        assert fill_dates([{"video_id": "vid001", "date": "2026-01-01"}]) == 0
    assert _viewlyt_warnings(caplog) == []


def test_fill_dates_warning_message_is_bounded(fake_yt, caplog: pytest.LogCaptureFixture) -> None:
    fake_yt(FakeYt(make_uploads(3), errors={"vid000": RuntimeError("x" * 5000)}))
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        fill_dates([{"video_id": "vid000"}])
    (rec,) = _viewlyt_warnings(caplog)
    assert len(rec.getMessage()) < 400


# --- fill_dates: an outage stops the batch instead of costing every timeout -----------


def _dated_items(n: int) -> list[dict]:
    return [{"video_id": f"vid{i:03d}"} for i in range(n)]


def test_fill_dates_stops_starting_requests_after_three_network_errors_in_a_row(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    # `--dates` on a 200-video window used to cost ~200/4 socket timeouts during an outage.
    yt = fake_yt(FakeYt(make_uploads(40), fail_pattern=lambda n: network_error()))
    items = _dated_items(40)
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(items, workers=1) == 0
    assert len(yt.watch_calls) == 3  # the breaker trips on the third, nothing is sent after
    assert all("date" not in it for it in items)
    (rec,) = _viewlyt_warnings(caplog)
    msg = rec.getMessage()
    assert "40 of 40" in msg and "3 request(s) failed" in msg
    assert "stopped after 3 network-level errors in a row" in msg
    assert "37 video(s) not attempted" in msg


def test_fill_dates_outage_with_several_workers_lets_only_the_running_requests_finish(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    yt = fake_yt(FakeYt(make_uploads(60), fail_pattern=lambda n: network_error()))
    items = _dated_items(60)
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(items, workers=4) == 0
    sent = len(yt.watch_calls)
    assert 3 <= sent <= 3 + 3  # at most the other workers' requests already in flight
    (rec,) = _viewlyt_warnings(caplog)
    msg = rec.getMessage()
    assert f"{sent} request(s) failed" in msg and f"{60 - sent} video(s) not attempted" in msg


def test_fill_dates_unrecognised_errors_count_as_network_level(fake_yt) -> None:
    # Same convention as the window probe: only a verdict about the video is exempt.
    yt = fake_yt(FakeYt(make_uploads(20), fail_pattern=lambda n: RuntimeError("boom")))
    assert fill_dates(_dated_items(20), workers=1) == 0
    assert len(yt.watch_calls) == 3


def test_fill_dates_keeps_what_it_dated_before_the_outage(fake_yt) -> None:
    yt = fake_yt(
        FakeYt(make_uploads(20), fail_pattern=lambda n: network_error() if n >= 5 else None)
    )
    items = _dated_items(20)
    assert fill_dates(items, workers=1) == 5
    assert len(yt.watch_calls) == 5 + 3
    assert [it.get("date") for it in items[:5]] == [day(i).isoformat() for i in range(5)]
    assert all("date" not in it for it in items[5:])


def test_fill_dates_two_network_errors_then_a_success_never_trip_the_breaker(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    # fail, fail, ok, fail, fail, ok, ...: never three in a row, so every video is tried.
    yt = fake_yt(
        FakeYt(make_uploads(12), fail_pattern=lambda n: network_error() if n % 3 != 2 else None)
    )
    items = _dated_items(12)
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(items, workers=1) == 4
    assert len(yt.watch_calls) == 12
    (rec,) = _viewlyt_warnings(caplog)
    assert "8 of 12" in rec.getMessage() and "not attempted" not in rec.getMessage()


def test_fill_dates_a_per_video_verdict_resets_the_network_error_count(fake_yt) -> None:
    script = {
        0: network_error(),
        1: network_error(),
        2: per_video_error(),  # YouTube answered: the next two are separate blips
        3: network_error(),
        4: network_error(),
    }
    yt = fake_yt(FakeYt(make_uploads(10), fail_pattern=lambda n: script.get(n)))
    assert fill_dates(_dated_items(10), workers=1) == 5  # calls 5..9 succeed
    assert len(yt.watch_calls) == 10


def test_fill_dates_per_video_verdicts_alone_never_stop_the_batch(
    fake_yt, caplog: pytest.LogCaptureFixture
) -> None:
    yt = fake_yt(FakeYt(make_uploads(30), fail_pattern=lambda n: per_video_error()))
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert fill_dates(_dated_items(30), workers=2) == 0
    assert len(yt.watch_calls) == 30  # every video was tried: each one is its own verdict
    (rec,) = _viewlyt_warnings(caplog)
    assert "30 request(s) failed" in rec.getMessage()
    assert "not attempted" not in rec.getMessage()


# ---------------------------------------------------------------------------
# lazy-import contract
# ---------------------------------------------------------------------------


def test_window_code_never_imports_yt_dlp_or_selenium() -> None:
    """`import viewlyt.ytdl`/`htmltext` stay cheap, and the new functions only reach
    yt-dlp through `_extract` (patched here), so `yt_dlp` is never imported."""
    code = (
        "import sys\n"
        "import viewlyt.ytdl as y\n"
        "import viewlyt.htmltext\n"
        "assert 'yt_dlp' not in sys.modules, 'yt_dlp imported at import time'\n"
        "assert 'selenium' not in sys.modules, 'selenium imported at import time'\n"
        "from datetime import date, timedelta\n"
        "d0 = date(2026, 6, 30)\n"
        "def fake(url, opts):\n"
        "    if url.endswith('/videos'):\n"
        "        n = opts['playlistend']\n"
        "        return {'channel': 'C', 'entries': [{'id': f'v{i:03d}'} for i in range(min(n, 90))]}\n"
        "    i = int(url.split('v=v')[1])\n"
        "    return {'upload_date': (d0 - timedelta(days=i)).strftime('%Y%m%d')}\n"
        "y._extract = fake\n"
        "_, items, _ = y.channel_entries_window('@c', d0 - timedelta(days=40), d0 - timedelta(days=5))\n"
        "assert len(items) == 36, len(items)\n"
        "undated = sum('date' not in it for it in items)\n"
        "assert undated > 0 and y.fill_dates(items) == undated\n"
        "assert y.video_upload_date('v001') == d0 - timedelta(days=1)\n"
        "try:\n"
        "    y.scan_window(lambda n: [{'id': f'x{i}'} for i in range(n)], lambda e: None, d0, d0)\n"
        "except y.WindowScanError:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('streak guard did not trip')\n"
        "assert 'yt_dlp' not in sys.modules, 'yt_dlp imported by the window functions'\n"
        "assert 'selenium' not in sys.modules\n"
        "print('OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
