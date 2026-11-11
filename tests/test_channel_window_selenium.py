"""``collect_channel_videos`` window mode (``since``/``until``) — the Selenium fallback of
``vl -C``. Browser-free: a fake driver stands in for the grid (one dispatcher keyed on the
module's own script constants), so these pin the Python side — what an age label means,
interval-overlap filtering, early stop, ``order``/``limit`` semantics, runaway guards. The
page-side JS (selectors, de-dup, last-date pick) is pinned by the opt-in e2e at the bottom,
against a synthetic DOM that mirrors the structure probed live on a real channel grid.

The labels used here are the ones the real grid renders (measured on live channels,
2026-11-11): ``Nh`` under a day, ``Nd`` up to ``13d`` (there is no ``1w``), ``2w``..``4w``,
``1mo``..``11mo``, then ``Ny``. YouTube FLOORS the unit, so "1y ago" is anywhere between
365 and ~730 days old — which is why the window logic works on intervals.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import E2E
from selenium.common.exceptions import WebDriverException

import viewlyt.scraper as scraper
from viewlyt.scraper import _card_span, collect_channel_videos

TODAY = date(2026, 11, 11)


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def _label(age: int) -> str:
    """The label the real grid shows for a video ``age`` days old (thresholds measured live)."""
    if age < 1:
        return "5h ago"
    if age < 14:
        return f"{age}d ago"
    if age < 30.4375:
        return f"{age // 7}w ago"
    if age < 365:
        return f"{int(age / 30.4375)}mo ago"
    return f"{int(age / 365.25)}y ago"


def _vid(i: int) -> str:
    return f"v{i:010d}"  # 11 chars, like a real video id


def _grid(ages: list[int]) -> list[tuple[str, str]]:
    """``(video_id, label)`` per card for videos of the given true ages (days), in the DOM
    order given — newest first, like the real ``/videos`` grid. The id encodes the age."""
    return [(_vid(a), _label(a)) for a in ages]


def _texts(labels: list[str]) -> list[tuple[str, str]]:
    return [(_vid(i), t) for i, t in enumerate(labels)]


def _age(video_id: str) -> int:
    """The age encoded in a `_grid` id (or, for `_texts` ids, the card's position)."""
    return int(video_id[1:])


class _GridDriver:
    """Fake driver for the channel ``/videos`` grid.

    Renders ``page`` cards, then ``grow`` more per ``window.scrollBy`` (the real grid's
    lazy continuation), out of the full ``cards`` list. ``_count`` answers ``2 *
    rendered`` on purpose: the real selector list matches every card twice (the
    rich-item and its nested lockup), which is why window mode counts distinct cards
    itself. The scripts are recognised by the scraper's own constants, so a renamed JS
    string can't silently desynchronise fake and code.
    """

    def __init__(
        self,
        cards: list[tuple[str, str]],
        *,
        page: int = 6,
        grow: int = 6,
        has_chip: bool = True,
        raise_after: int | None = None,
        probe_result: object = None,
        name: str = "Someone",
    ) -> None:
        self.cards = cards
        self.rendered = min(page, len(cards))
        self.grow = grow
        self.has_chip = has_chip
        self.raise_after = raise_after
        self.probe_result = probe_result
        self.name = name
        self.current_url = "https://www.youtube.com/@someone/videos"
        self.page_source = ""
        self.get_calls: list[str] = []
        self.script_calls = 0
        self.scrolls = 0
        self.probes = 0
        self.harvests = 0
        self.legacy_harvests = 0
        self.chip_calls = 0
        self.chip_clicked = False

    def get(self, url: str) -> None:
        self.get_calls.append(url)

    def execute_script(self, script: str, *args):
        self.script_calls += 1
        if self.raise_after is not None and self.script_calls > self.raise_after:
            raise WebDriverException("boom")
        if "var LABELS=arguments[0];" in script:  # _CONSENT_SCAN_JS: no dialog
            return None
        if 'meta[property="og:title"]' in script:  # _CHANNEL_NAME_JS
            return self.name
        if script == scraper._CHANNEL_DATES_JS:
            self.probes += 1
            if self.probe_result is not None:
                return self.probe_result
            return [text for _, text in self.cards[: self.rendered]]
        if script == scraper._CHANNEL_WINDOW_JS:
            self.harvests += 1
            if self.probe_result is not None:
                return None
            return [
                {
                    "video_id": vid,
                    "title": f"Title {vid}",
                    "views": "1 views",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "date_text": text,
                }
                for vid, text in self.cards[: self.rendered]
            ]
        if script == "window.scrollBy(0, 1200);":
            self.scrolls += 1
            self.rendered = min(len(self.cards), self.rendered + self.grow)
            return None
        if "SELS[s]" in script:  # _CLICK_CHANNEL_CHIP_JS
            self.chip_calls += 1
            self.chip_clicked = self.has_chip
            return self.has_chip
        if "for(var i=0;i<items.length;i++)" in script:  # _FIRST_CHANNEL_VIDEO_ID_JS
            return self.cards[0][0] if (self.chip_clicked or not self.has_chip) else "OLDID"
        if "document.querySelectorAll(arguments[0]).length" in script:  # _count
            return 2 * self.rendered
        if "out.push({video_id:id" in script:  # _RELATED_JS: the plain harvest
            self.legacy_harvests += 1
            limit = args[2]
            return [
                {
                    "video_id": vid,
                    "title": f"Title {vid}",
                    "views": "1 views",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }
                for vid, _ in self.cards[: self.rendered]
            ][:limit]
        return None


@pytest.fixture(autouse=True)
def _pin_environment(monkeypatch):
    monkeypatch.setattr(scraper, "_today", lambda: TODAY)
    monkeypatch.setattr(scraper, "_sleep_jitter", lambda *a, **k: None)
    monkeypatch.setattr(scraper, "dismiss_consent_dialog", lambda *a, **k: False)


def _run(drv: _GridDriver, limit: int = 0, order: str = "recent", progress=False, **window):
    return collect_channel_videos(drv, "@someone", limit, order, progress, **window)


def _ids(items: list[dict]) -> list[str]:
    return [it["video_id"] for it in items]


# --------------------------------------------------------------------------- #
# Without a window: exactly the plain behaviour
# --------------------------------------------------------------------------- #
def test_no_window_items_are_unchanged_and_carry_no_date() -> None:
    drv = _GridDriver(_grid([1, 2, 3, 4, 5, 6, 7, 8]))
    name, items = _run(drv, limit=3)
    assert name == "Someone"
    assert _ids(items) == [_vid(1), _vid(2), _vid(3)]
    for it in items:
        assert set(it) == {"video_id", "title", "views", "url", "views_int"}
    # The window machinery (probe + date harvest) is never touched.
    assert (drv.probes, drv.harvests) == (0, 0)
    assert drv.legacy_harvests == 1


def test_no_window_nonpositive_limit_short_circuits_before_the_driver() -> None:
    for limit in (0, -3):
        drv = _GridDriver(_grid([1, 2]))
        assert _run(drv, limit=limit) == ("", [])
        assert drv.get_calls == [] and drv.script_calls == 0


@pytest.mark.parametrize("order", ["old", "popular"])
def test_no_window_still_clicks_the_sort_chip(order: str) -> None:
    drv = _GridDriver(_grid([1, 2, 3]))
    _run(drv, limit=2, order=order)
    assert drv.chip_clicked is True


# --------------------------------------------------------------------------- #
# What an age label means: an INTERVAL of possible dates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "newest_age", "oldest_age"),
    [
        ("5h ago", 0, 1),
        ("23h ago", 0, 1),
        ("10m ago", 0, 1),  # "m" is minutes, "mo" is months
        ("30s ago", 0, 1),
        ("Just now", 0, 1),
        ("1d ago", 1, 2),
        ("6d ago", 6, 7),
        ("13d ago", 13, 14),
        ("2w ago", 14, 21),
        ("3w ago", 21, 28),
        ("4w ago", 28, 35),
        ("1mo ago", 27, 64),
        ("5mo ago", 149, 186),
        ("11mo ago", 331, 369),
        ("1y ago", 365, 732),
        ("2y ago", 730, 1097),
        ("16y ago", 5844, 6211),
        # long forms, a/an, "(edited)" suffix and Streamed/Premiered prefixes
        ("2 weeks ago", 14, 21),
        ("3 months ago", 88, 125),
        ("1 year ago", 365, 732),
        ("Streamed 3 weeks ago", 21, 28),
        ("Streamed a month ago", 27, 64),
        ("Streamed live 2 days ago", 2, 3),
        ("Premiered an hour ago", 0, 1),
        ("  3 weeks ago (edited) ", 21, 28),
    ],
)
def test_card_span_is_the_interval_the_label_can_mean(
    text: str, newest_age: int, oldest_age: int
) -> None:
    assert _card_span(text, TODAY) == (_ago(oldest_age), _ago(newest_age))


# Ground truth: for every label the real grids rendered, the date-to-date ages (days) of the
# videos behind it, read from yt-dlp upload dates on 2026-11-11 (@veritasium and @BBCNews,
# newest and oldest card of each label). Each must fall inside that label's interval — the
# regression for reading "1y ago" as exactly 365 days.
_LIVE_AGES = {
    "2h ago": [0],
    "13h ago": [1],
    "21h ago": [1],
    "1d ago": [1, 2],
    "2d ago": [2, 3],
    "6d ago": [6, 7],
    "7d ago": [7, 8],
    "13d ago": [13, 14],
    "2w ago": [14, 21],
    "3w ago": [21, 28],
    "4w ago": [28, 31],
    "1mo ago": [31, 51],
    "2mo ago": [86],
    "3mo ago": [101, 120],
    "4mo ago": [142, 150],
    "5mo ago": [159, 165],
    "6mo ago": [192, 210],
    "7mo ago": [220, 237],
    "8mo ago": [247, 267],
    "9mo ago": [281, 296],
    "10mo ago": [315, 327],
    "11mo ago": [341, 364],
    "1y ago": [376, 728],
    "2y ago": [737, 1092],
    "3y ago": [1103, 1457],
    "4y ago": [1469, 1818],
    "5y ago": [1836, 2172],
    "6y ago": [2202, 2548],
    "7y ago": [2574, 2918],
    "8y ago": [2931, 3278],
    "9y ago": [3313, 3635],
    "10y ago": [3661, 4018],
    "11y ago": [4046, 4363],
    "12y ago": [4389, 4736],
    "13y ago": [4753],
    "14y ago": [5118, 5465],
    "15y ago": [5479, 5787],
    "16y ago": [5931, 5932],
}


@pytest.mark.parametrize(("label", "ages"), list(_LIVE_AGES.items()))
def test_card_span_contains_the_real_dates_behind_live_labels(label: str, ages: list[int]) -> None:
    oldest, newest = _card_span(label, TODAY)
    for age in ages:
        assert oldest <= _ago(age) <= newest, (label, age)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "17M",
        "1.2M views",
        "Scheduled for 12/12",
        "1.2K watching",
        "Streamed live",
        "há 6 dias",
        "hace 3 semanas",
        "vor 2 Wochen",
    ],
)
def test_card_span_is_none_for_non_ages(text: str) -> None:
    assert _card_span(text, TODAY) is None


def test_card_span_never_raises_on_absurd_counts() -> None:
    assert _card_span("999999999999999 years ago", TODAY) is None
    assert _card_span("99999999999 days ago", date(2000, 1, 1)) is None
    assert _card_span(("9" * 5000) + " days ago", TODAY) is None


# --------------------------------------------------------------------------- #
# Window filtering: keep whatever MIGHT be inside
# --------------------------------------------------------------------------- #
def test_floored_year_labels_still_return_the_cards_of_a_past_window() -> None:
    # The defect: every 2025-H1 video is labelled "1y ago", which as a single date is
    # 2025-11-11 (> until, dropped), while the 2y labels read 2024-11-11 (< since, dropped):
    # an empty result although the channel published in the window.
    since, until = date(2025, 1, 1), date(2025, 6, 30)
    ages = list(range(1, 1500, 9))
    cards = _grid(ages)
    in_window = {_vid(a) for a in ages if since <= _ago(a) <= until}
    assert in_window  # the scenario is non-trivial
    drv = _GridDriver(cards)
    _, items = _run(drv, since=since, until=until)
    assert in_window <= set(_ids(items))
    # ...and the only extras are the cards whose label could still mean the window.
    assert {_label(_age(i)) for i in _ids(items)} == {"1y ago"}
    assert {it["date"] for it in items} == {_ago(365).isoformat()}  # the NEWEST possible date
    assert drv.rendered < len(cards)  # the first "2y ago" card ended the scroll


def test_old_order_returns_the_same_window_oldest_first() -> None:
    since, until = date(2025, 1, 1), date(2025, 6, 30)
    cards = _grid(list(range(1, 1500, 9)))
    _, recent = _run(_GridDriver(cards), since=since, until=until)
    _, old = _run(_GridDriver(cards), order="old", since=since, until=until)
    assert recent and _ids(old) == _ids(recent)[::-1]


def test_window_edges_keep_every_card_that_might_be_inside() -> None:
    # until = 7 days ago: a "6d ago" card is 6 or 7 days old (the day jitter), so it stays;
    # a "5d ago" card is 5 or 6 days old: certainly too new. since = 14 days ago: "2w ago"
    # is 14..21 days old, so it stays; "3w ago" (21..28) is certainly too old.
    labels = ["1d ago", "5d ago", "6d ago", "7d ago", "13d ago", "2w ago", "3w ago", "4w ago"]
    drv = _GridDriver(_texts(labels), page=20)
    _, items = _run(drv, since=_ago(14), until=_ago(7))
    assert [labels[_age(i)] for i in _ids(items)] == ["6d ago", "7d ago", "13d ago", "2w ago"]


def test_one_sided_windows() -> None:
    labels = ["1d ago", "7d ago", "2w ago", "1mo ago", "2mo ago"]
    cards = _texts(labels)
    _, since_only = _run(_GridDriver(cards, page=20), since=_ago(14))
    assert [labels[_age(i)] for i in _ids(since_only)] == ["1d ago", "7d ago", "2w ago"]
    _, until_only = _run(_GridDriver(cards, page=20), until=_ago(7))
    assert [labels[_age(i)] for i in _ids(until_only)] == ["7d ago", "2w ago", "1mo ago", "2mo ago"]


def test_item_date_is_the_newest_possible_date() -> None:
    labels = ["2d ago", "5mo ago", "1y ago"]
    _, items = _run(_GridDriver(_texts(labels), page=10), since=_ago(2000))
    assert [it["date"] for it in items] == [
        _ago(2).isoformat(),
        _ago(149).isoformat(),
        _ago(365).isoformat(),
    ]


def test_window_item_shape_has_date_and_no_leaked_raw_text() -> None:
    _, items = _run(_GridDriver(_grid([2])), since=_ago(5))
    assert items == [
        {
            "video_id": _vid(2),
            "title": f"Title {_vid(2)}",
            "views": "1 views",
            "url": f"https://www.youtube.com/watch?v={_vid(2)}",
            "date": _ago(2).isoformat(),
            "views_int": 1,
        }
    ]


def test_streamed_and_compact_labels_through_the_whole_function() -> None:
    labels = ["6d ago", "Streamed 2 weeks ago", "Premiered a month ago", "3mo ago"]
    _, items = _run(_GridDriver(_texts(labels), page=10), since=_ago(300))
    assert len(items) == 4
    assert [it["date"] for it in items] == [
        _ago(6).isoformat(),
        _ago(14).isoformat(),
        _ago(27).isoformat(),
        _ago(88).isoformat(),
    ]


def test_a_few_undated_cards_among_dated_ones_are_kept_with_empty_date() -> None:
    labels = ["1d ago", "", "3d ago", "Scheduled for tomorrow", "5d ago", "2mo ago"]
    _, items = _run(_GridDriver(_texts(labels), page=10), since=_ago(30))
    assert [(labels[_age(it["video_id"])], bool(it["date"])) for it in items] == [
        ("1d ago", True),
        ("", False),
        ("3d ago", True),
        ("Scheduled for tomorrow", False),
        ("5d ago", True),
    ]  # the "2mo ago" card (57..95 days) is certainly older than the 30-day window


# --------------------------------------------------------------------------- #
# Early stop
# --------------------------------------------------------------------------- #
def test_recent_stops_scrolling_once_even_the_newest_possible_date_is_before_since() -> None:
    # 400 daily videos, 6 more cards per scroll: without the early stop this would scroll
    # ~65 times; with it, only until the rendered tail is certainly older than 30 days.
    cards = _grid(list(range(1, 401)))
    drv = _GridDriver(cards, page=6, grow=6)
    _, items = _run(drv, since=_ago(30))
    assert _label(30) == "4w ago" and _label(31) == "1mo ago"
    assert max(_age(i) for i in _ids(items)) >= 30  # nothing that might be inside is lost
    assert set(range(1, 31)) <= {_age(i) for i in _ids(items)}
    assert drv.rendered < 100 and drv.scrolls < 15


def test_early_stop_waits_for_the_newest_end_not_the_oldest() -> None:
    # The "1y ago" card is 365..~730 days old: it may well be inside a window that starts
    # 500 days ago, so scrolling must go on to the "2y ago" card, which certainly is not.
    cards = _grid([1, 3, 5, 400, 900])
    drv = _GridDriver(cards, page=4, grow=1)
    _, items = _run(drv, since=_ago(500))
    assert [_label(_age(i)) for i in _ids(items)] == ["1d ago", "3d ago", "5d ago", "1y ago"]
    assert drv.scrolls == 1


def test_recent_early_stop_uses_the_last_dated_card() -> None:
    labels = ["1d ago", "2d ago", "3d ago", "2mo ago", ""]  # undated tail card
    drv = _GridDriver(_texts(labels), page=5, grow=5)
    _, items = _run(drv, since=_ago(30))
    assert drv.scrolls == 0  # the "2mo ago" card already proves everything further is older
    assert [labels[_age(i)] for i in _ids(items)] == ["1d ago", "2d ago", "3d ago", ""]


def test_popular_has_no_date_stop_it_only_filters() -> None:
    # Not date-sorted: with 2 cards rendered the last one ("1y ago") is already certainly
    # older than `since`, which would end a `recent` scan right there and lose the later
    # in-window cards.
    cards = _grid([3, 400, 5, 20, 90])
    drv = _GridDriver(cards, page=2, grow=2)
    _, items = _run(drv, order="popular", since=_ago(30))
    assert drv.chip_clicked is True
    assert _ids(items) == [_vid(3), _vid(5), _vid(20)]


# --------------------------------------------------------------------------- #
# order="old": the newest-first grid is walked and reversed, the chip never clicked
# --------------------------------------------------------------------------- #
def test_window_old_never_clicks_the_chip_and_returns_oldest_first() -> None:
    cards = _grid(list(range(1, 70)))
    _, recent = _run(_GridDriver(cards), since=_ago(40), until=_ago(5))
    drv = _GridDriver(cards)
    _, old = _run(drv, order="old", since=_ago(40), until=_ago(5))
    assert drv.chip_calls == 0
    assert recent and _ids(old) == _ids(recent)[::-1]
    assert [_age(i) for i in _ids(old)] == sorted((_age(i) for i in _ids(old)), reverse=True)


def test_window_old_without_a_sort_chip_is_no_longer_empty() -> None:
    # Missing chip used to leave the newest-first grid believed oldest-first: the first
    # probe's last card looked "newer than until" and the scan ended with nothing.
    cards = _grid(list(range(1, 70)))
    drv = _GridDriver(cards, has_chip=False)
    _, old = _run(drv, order="old", since=_ago(40), until=_ago(5))
    assert old and _age(old[0]["video_id"]) > _age(old[-1]["video_id"])


def test_window_old_with_only_since_reaches_the_window_without_the_chip() -> None:
    # `since` alone puts the window at the far END of an oldest-first grid; walking the
    # newest-first grid reaches it via the date stop instead.
    cards = _grid(list(range(1, 400)))
    drv = _GridDriver(cards, page=10, grow=10)
    _, recent = _run(_GridDriver(cards, page=10, grow=10), since=_ago(60))
    _, old = _run(drv, order="old", since=_ago(60))
    assert recent and _ids(old) == _ids(recent)[::-1]
    assert drv.chip_calls == 0 and drv.rendered < len(cards)


def test_window_old_limit_means_the_n_oldest_of_the_window() -> None:
    cards = _grid(list(range(1, 70)))
    _, full_old = _run(_GridDriver(cards), order="old", since=_ago(40), until=_ago(5))
    drv = _GridDriver(cards)
    _, old3 = _run(drv, limit=3, order="old", since=_ago(40), until=_ago(5))
    assert _ids(old3) == _ids(full_old)[:3]
    # No limit stop for `old`: the oldest end is only known once the date stop is reached.
    assert max(_age(i) for i in _ids(old3)) >= 40
    assert drv.rendered > 40


# --------------------------------------------------------------------------- #
# limit semantics (recent)
# --------------------------------------------------------------------------- #
def test_limit_caps_after_filtering_not_before() -> None:
    # until = 10 days ago: cards aged 1..8 are certainly too new (a "9d ago" card is 9 or
    # 10 days old, so it stays). A pre-filter cap of 5 would return nothing.
    cards = _grid(list(range(1, 41)))
    drv = _GridDriver(cards, page=6, grow=6)
    _, items = _run(drv, limit=5, since=_ago(30), until=_ago(10))
    assert [_age(i) for i in _ids(items)] == [9, 10, 11, 12, 13]
    assert drv.scrolls <= 2  # stops at the 5th kept card, not at the window's far edge


def test_limit_zero_with_a_window_means_no_count_limit() -> None:
    cards = _grid(list(range(1, 101)))
    name, items = _run(_GridDriver(cards, page=10, grow=10), limit=0, since=_ago(30))
    assert name == "Someone"
    assert set(range(1, 31)) <= {_age(i) for i in _ids(items)}


def test_negative_limit_with_a_window_also_means_no_count_limit() -> None:
    cards = _grid(list(range(1, 41)))
    _, none = _run(_GridDriver(cards), limit=-1, since=_ago(10))
    _, zero = _run(_GridDriver(cards), limit=0, since=_ago(10))
    assert _ids(none) == _ids(zero) and none


def test_limit_larger_than_the_window_returns_the_whole_window() -> None:
    cards = _grid(list(range(1, 41)))
    _, big = _run(_GridDriver(cards), limit=500, since=_ago(10))
    _, zero = _run(_GridDriver(cards), limit=0, since=_ago(10))
    assert _ids(big) == _ids(zero)


# --------------------------------------------------------------------------- #
# Unreadable ages: fail loudly instead of passing a channel off as a window
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("limit", [0, 5])
def test_wholesale_undated_grid_fails_with_a_warning_before_any_scrolling(
    limit: int, caplog
) -> None:
    # A non-English UI ("há 6 dias"): every card is "undated", which used to be "kept" —
    # up to 150 scroll steps of the whole channel returned as the window. With a limit the
    # undated cards would also satisfy the limit stop, so the check has to come first.
    drv = _GridDriver(_texts(["há 6 dias"] * 10), page=10)
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        assert _run(drv, limit=limit, since=_ago(30)) == ("", [])
    assert (drv.probes, drv.scrolls, drv.harvests) == (1, 0, 0)
    assert "@someone" in caplog.text and "cannot read the publish ages" in caplog.text
    assert "non-English" in caplog.text


def test_more_than_half_undated_is_the_threshold() -> None:
    dated = ["1d ago", "2d ago", "3d ago", "4d ago", "5d ago"]
    half = [x for pair in zip(dated, [""] * 5, strict=True) for x in pair]  # 5 of 10 undated
    _, items = _run(_GridDriver(_texts(half), page=10), since=_ago(30))
    assert len(items) == 10
    over = ["1d ago", "", "2d ago", "", "3d ago", "", "4d ago", "", "", ""]  # 6 of 10 undated
    assert _run(_GridDriver(_texts(over), page=10), since=_ago(30)) == ("", [])


def test_too_few_cards_to_judge_the_dates_are_kept() -> None:
    _, items = _run(_GridDriver(_texts(["", "", "", ""]), page=10), since=_ago(30))
    assert [it["date"] for it in items] == ["", "", "", ""]


# --------------------------------------------------------------------------- #
# Runaway guards and failure modes
# --------------------------------------------------------------------------- #
def test_step_budget_bounds_a_grid_that_never_settles_and_says_so(monkeypatch, caplog) -> None:
    monkeypatch.setattr(scraper, "_CHANNEL_WINDOW_MAX_STEPS", 7)
    # Every card is 1 day old, so a `since` of 5 days ago is never undercut and the grid
    # keeps growing: only the budget can end this.
    drv = _GridDriver([(_vid(i), "1d ago") for i in range(500)], page=1, grow=1)
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        _, items = _run(drv, since=_ago(5))
    assert drv.scrolls == 7
    assert len(items) == 8
    assert "@someone" in caplog.text and "incomplete" in caplog.text
    assert "7 steps" in caplog.text and "7 cards" in caplog.text


def test_no_incomplete_warning_when_the_scan_ends_normally(caplog) -> None:
    cards = _grid(list(range(1, 400)))
    with caplog.at_level(logging.WARNING, logger="viewlyt"):
        _run(_GridDriver(cards), since=_ago(30))  # date stop
        _run(_GridDriver(cards), limit=3, since=_ago(30))  # limit stop
        _run(_GridDriver(_grid([1, 2, 3, 4]), page=4, grow=0), since=_ago(5))  # grid end
    assert "incomplete" not in caplog.text


def test_stagnant_grid_stops_after_the_stale_tolerance() -> None:
    drv = _GridDriver(_grid([1, 2, 3, 4]), page=4, grow=0)
    _, items = _run(drv, since=_ago(5))
    assert drv.scrolls == scraper._CHANNEL_WINDOW_STALE
    assert len(items) == 4


@pytest.mark.parametrize("raise_after", [0, 1, 2, 3, 4, 5, 6, 8, 12])
def test_driver_failing_midway_never_raises(raise_after: int) -> None:
    drv = _GridDriver(_grid(list(range(1, 100))), raise_after=raise_after)
    name, items = _run(drv, since=_ago(60))
    assert isinstance(name, str)
    assert isinstance(items, list)
    if raise_after < 4:  # died before the harvest could run: the plain failure result
        assert (name, items) == ("", [])


def test_driver_failing_on_navigation_returns_the_plain_failure_result() -> None:
    class _Boom(_GridDriver):
        def get(self, url: str) -> None:
            raise WebDriverException("boom")

    assert _run(_Boom(_grid([1, 2])), since=_ago(5)) == ("", [])


def test_garbage_probe_and_harvest_payloads_do_not_raise() -> None:
    drv = _GridDriver(_grid([1, 2, 3]), probe_result="not-a-list")
    name, items = _run(drv, since=_ago(5))
    assert (name, items) == ("Someone", [])
    assert drv.scrolls == scraper._CHANNEL_WINDOW_STALE  # a dateless probe can't prove a stop


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def test_window_mode_logs_the_window_not_a_zero_count(caplog) -> None:
    cards = _grid(list(range(1, 40)))
    with caplog.at_level(logging.INFO, logger="viewlyt"):
        _run(_GridDriver(cards), progress=True, since=date(2025, 1, 1), until=date(2025, 6, 30))
    assert "2025-01-01..2025-06-30" in caplog.text
    assert "up to 0" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="viewlyt"):
        _run(_GridDriver(cards), limit=5, progress=True, since=_ago(30))
    assert "up to 5" in caplog.text


def test_plain_mode_log_line_is_unchanged(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="viewlyt"):
        _run(_GridDriver(_grid([1, 2, 3])), limit=2, progress=True)
    assert "collecting up to 2 channel videos (recent) from @someone" in caplog.text


# --------------------------------------------------------------------------- #
# Selector hygiene / drift guards
# --------------------------------------------------------------------------- #
def test_date_selectors_are_exact_tokens_no_class_substring() -> None:
    forbidden = "[class*="
    for css in scraper.CHANNEL_DATE_SELECTORS:
        assert forbidden not in css
        assert "*=" not in css and "^=" not in css and "$=" not in css
    for js in (
        scraper._CHANNEL_CARD_DATE_JS,
        scraper._CHANNEL_DATES_JS,
        scraper._CHANNEL_WINDOW_JS,
    ):
        assert forbidden not in js
    assert forbidden not in Path(scraper.__file__).read_text(encoding="utf-8")


def test_window_harvest_keeps_the_related_harvest_selectors() -> None:
    # _CHANNEL_WINDOW_JS copies _RELATED_JS's card harvest (the latter is shared with the
    # related-videos product and locked): a selector fix landing in only one of them would
    # make the two silently diverge.
    for needle in (
        "a[href*='/watch?v=']",
        "yt-content-metadata-view-model",
        "/view|visualiz/i",
        "[A-Za-z0-9_-]{11}",
        "a.getAttribute('title')",
    ):
        assert needle in scraper._RELATED_JS, needle
        assert needle in scraper._CHANNEL_WINDOW_JS, needle


# --------------------------------------------------------------------------- #
# The page-side JS, against a synthetic DOM shaped like the real grid (opt-in)
# --------------------------------------------------------------------------- #
# Structure as probed live on a channel /videos grid (2026-11): every card is a
# <ytd-rich-item-renderer> wrapping a <yt-lockup-view-model> (so the grid selector list
# matches it twice); the metadata row holds an icon span, role=text spans separated by a
# delimiter span, and the LAST text span is the COMPACT relative date. Collabs prepend
# channel-name spans. (The views span is the bare "17M" there.)
_FAKE_GRID_HTML = """
<div id="contents">
 <ytd-rich-item-renderer><yt-lockup-view-model>
  <a href="/watch?v=AAAAAAAAAAA" title="First video"></a>
  <h3><a href="/watch?v=AAAAAAAAAAA">First video</a></h3>
  <yt-content-metadata-view-model><div role="group">
   <span class="ytIconWrapperHost"><svg></svg></span>
   <span role="text" class="ytAttributedStringHost">17M</span>
   <span class="ytContentMetadataViewModelDelimiter"></span>
   <span role="text" class="ytAttributedStringHost ytContentMetadataViewModelMetadataTextLastPart">6d ago</span>
  </div></yt-content-metadata-view-model>
 </yt-lockup-view-model></ytd-rich-item-renderer>
 <ytd-rich-item-renderer><yt-lockup-view-model>
  <h3><a href="/watch?v=BBBBBBBBBBB">Collab video</a></h3>
  <yt-content-metadata-view-model><div role="group">
   <span role="text">Veritasium</span><span role="text">and 2swap</span>
   <span role="text">8.9M</span>
   <span role="text" class="ytContentMetadataViewModelMetadataTextLastPart">5mo ago</span>
  </div></yt-content-metadata-view-model>
 </yt-lockup-view-model></ytd-rich-item-renderer>
 <ytd-rich-item-renderer><yt-lockup-view-model>
  <h3><a href="/watch?v=CCCCCCCCCCC">Legacy markup</a></h3>
  <yt-content-metadata-view-model><div role="group">
   <span role="text">1.2M views</span><span role="text">3 weeks ago</span>
  </div></yt-content-metadata-view-model>
 </yt-lockup-view-model></ytd-rich-item-renderer>
 <ytd-rich-item-renderer><yt-lockup-view-model>
  <h3><a href="/watch?v=DDDDDDDDDDD">No metadata</a></h3>
 </yt-lockup-view-model></ytd-rich-item-renderer>
</div>
"""


@pytest.mark.e2e
@E2E
def test_e2e_window_js_against_a_synthetic_grid() -> None:
    from viewlyt.driver import build_driver

    driver = build_driver(headless=True)
    try:
        driver.get("about:blank")
        driver.execute_script("document.body.innerHTML = arguments[0];", _FAKE_GRID_HTML)
        sel = ", ".join(scraper.CHANNEL_ITEM_SELECTORS)
        date_sels = list(scraper.CHANNEL_DATE_SELECTORS)
        # 8 elements match the grid selector list; the probe counts the 4 distinct cards.
        assert scraper._count(driver, sel) == 8
        assert driver.execute_script(scraper._CHANNEL_DATES_JS, sel, date_sels) == [
            "6d ago",
            "5mo ago",
            "3 weeks ago",
            "",
        ]
        raw = driver.execute_script(
            scraper._CHANNEL_WINDOW_JS, sel, list(scraper.RELATED_TITLE_SELECTORS), date_sels
        )
    finally:
        driver.quit()
    assert [(r["video_id"], r["title"], r["date_text"]) for r in raw] == [
        ("AAAAAAAAAAA", "First video", "6d ago"),
        ("BBBBBBBBBBB", "Collab video", "5mo ago"),
        ("CCCCCCCCCCC", "Legacy markup", "3 weeks ago"),
        ("DDDDDDDDDDD", "No metadata", ""),
    ]
    assert raw[2]["views"] == "1.2M views"
    assert raw[0]["url"] == "https://www.youtube.com/watch?v=AAAAAAAAAAA"
