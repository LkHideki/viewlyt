"""Browser-free, network-free tests for the channel-window core.

Covers the pure pieces (``htmltext.parse_window_spec``, the optional ``date`` in
``format_related``), the injected-I/O search (``ytdl.scan_window``) and the
yt-dlp wrappers around it (``video_upload_date``, ``channel_entries_window``,
``fill_dates``), the latter through a fake of ``ytdl._extract`` that honours
``playlistend`` — yt-dlp itself is never imported.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
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


def test_scan_all_entries_undatable_keeps_everything_and_never_crashes() -> None:
    dates: list[date | None] = [None] * 100
    ch = FakeChannel(dates)
    got, meta, items = ch.scan(day(50), day(10))
    assert got == list(range(100))
    assert all("_date" not in e for e in items)
    assert len(ch.probed) == len(set(ch.probed))  # still never a repeated probe
    assert meta["non_monotonic"] is False


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


def test_scan_inversion_caught_by_the_gallop_is_dated_linearly() -> None:
    dates: list[date | None] = list(daily(40))
    dates[15] = NEWEST + timedelta(days=2)  # a premiere/VOD listed far below where it belongs
    ch = FakeChannel(dates)
    since, until = day(30), day(10)
    got, meta, _ = ch.scan(since, until, first_batch=8)
    assert meta["non_monotonic"] is True
    assert got == brute(dates, since, until)  # entry 15 is positionally inside but dated outside
    assert 15 not in got
    assert len(ch.probed) == len(set(ch.probed))


def test_scan_inversion_caught_by_the_bisection_is_dated_linearly() -> None:
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
    ) -> None:
        self.uploads = uploads
        self.name = name
        self.broken = broken or set()
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
                }
                for i, (vid, _) in enumerate(self.uploads[:n])
            ]
            return {"channel": self.name, "entries": entries}
        vid = url.split("v=")[1]
        if vid in self.broken:
            raise RuntimeError(f"extraction failed for {vid}")
        d = dict(self.uploads)[vid]
        return {"upload_date": d.strftime("%Y%m%d") if d else None}


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
    assert seen == [("https://www.youtube.com/watch?v=abcdefghijk", {})]


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
    # Every probe is one watch-page extraction with no options, and none repeats.
    assert len(yt.watch_calls) == len(set(yt.watch_calls)) == meta["probes"]
    assert all(c[1] == {} for c in yt.calls if "watch?v=" in c[0])


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


def test_channel_entries_window_since_after_until_does_no_io(fake_yt) -> None:
    yt = fake_yt(FakeYt(make_uploads(50)))
    name, items, meta = channel_entries_window("@chan", day(10), day(30))
    assert (name, items) == ("", [])
    assert yt.calls == [] and meta["scanned"] == 0


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
        "assert 'yt_dlp' not in sys.modules, 'yt_dlp imported by the window functions'\n"
        "assert 'selenium' not in sys.modules\n"
        "print('OK')\n"
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout
