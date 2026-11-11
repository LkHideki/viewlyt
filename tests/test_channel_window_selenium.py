"""``collect_channel_videos`` window mode (``since``/``until``) — the Selenium fallback of
``vl -C``. Browser-free: a fake driver stands in for the grid (one dispatcher keyed on the
module's own script constants), so these pin the Python side — date parsing, inclusive
filtering, early stop, limit semantics, runaway guards. The page-side JS (selectors,
de-dup, last-date pick) is pinned by the opt-in e2e at the bottom, against a synthetic
DOM that mirrors the structure probed live on a real channel grid.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import E2E
from selenium.common.exceptions import WebDriverException

import viewlyt.scraper as scraper
from viewlyt.scraper import _card_date, collect_channel_videos

TODAY = date(2026, 11, 11)


def _ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def _vid(i: int) -> str:
    return f"v{i:010d}"  # 11 chars, like a real video id


def _cards(ages: list[int], fmt: str = "{} days ago") -> list[tuple[str, str]]:
    """``(video_id, relative-date text)`` per card, in the DOM order given."""
    return [(_vid(a), fmt.format(a)) for a in ages]


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


def _run(drv: _GridDriver, limit: int = 0, order: str = "recent", **window):
    return collect_channel_videos(drv, "@someone", limit, order, False, **window)


# --------------------------------------------------------------------------- #
# Without a window: exactly the plain behaviour
# --------------------------------------------------------------------------- #
def test_no_window_items_are_unchanged_and_carry_no_date() -> None:
    drv = _GridDriver(_cards([1, 2, 3, 4, 5, 6, 7, 8]))
    name, items = _run(drv, limit=3)
    assert name == "Someone"
    assert [it["video_id"] for it in items] == [_vid(1), _vid(2), _vid(3)]
    for it in items:
        assert set(it) == {"video_id", "title", "views", "url", "views_int"}
    # The window machinery (probe + date harvest) is never touched.
    assert (drv.probes, drv.harvests) == (0, 0)
    assert drv.legacy_harvests == 1


def test_no_window_nonpositive_limit_short_circuits_before_the_driver() -> None:
    for limit in (0, -3):
        drv = _GridDriver(_cards([1, 2]))
        assert _run(drv, limit=limit) == ("", [])
        assert drv.get_calls == [] and drv.script_calls == 0


# --------------------------------------------------------------------------- #
# Window filtering
# --------------------------------------------------------------------------- #
def test_window_is_inclusive_on_both_edges() -> None:
    drv = _GridDriver(_cards([1, 7, 14, 30]))
    _, items = _run(drv, since=_ago(14), until=_ago(7))
    assert [it["date"] for it in items] == [_ago(7).isoformat(), _ago(14).isoformat()]
    # One day inside either edge stays in; one day outside is out.
    _, items = _run(_GridDriver(_cards([6, 7, 8, 13, 14, 15])), since=_ago(14), until=_ago(7))
    assert [it["video_id"] for it in items] == [_vid(a) for a in (7, 8, 13, 14)]


def test_one_sided_windows() -> None:
    cards = _cards([1, 7, 14, 30])
    _, since_only = _run(_GridDriver(cards), since=_ago(14))
    assert [it["video_id"] for it in since_only] == [_vid(1), _vid(7), _vid(14)]
    _, until_only = _run(_GridDriver(cards), until=_ago(7))
    assert [it["video_id"] for it in until_only] == [_vid(7), _vid(14), _vid(30)]


def test_window_item_shape_has_date_and_no_leaked_raw_text() -> None:
    _, items = _run(_GridDriver(_cards([2])), since=_ago(5))
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


def test_undated_cards_are_kept_with_empty_date() -> None:
    cards = [
        (_vid(1), "1 day ago"),
        (_vid(2), ""),  # no date node at all
        (_vid(3), "Scheduled for tomorrow"),  # not a relative age
        (_vid(4), "40 days ago"),
    ]
    _, items = _run(_GridDriver(cards), since=_ago(30))
    assert [(it["video_id"], it["date"]) for it in items] == [
        (_vid(1), _ago(1).isoformat()),
        (_vid(2), ""),
        (_vid(3), ""),
    ]


def test_streamed_and_compact_texts_through_the_whole_function() -> None:
    cards = [
        (_vid(1), "6d ago"),
        (_vid(2), "Streamed 2 weeks ago"),
        (_vid(3), "Premiered a month ago"),
        (_vid(4), "3mo ago"),
    ]
    _, items = _run(_GridDriver(cards), since=_ago(120))
    assert [it["date"] for it in items] == [
        _ago(6).isoformat(),
        _ago(14).isoformat(),
        _ago(30).isoformat(),
        _ago(90).isoformat(),
    ]


@pytest.mark.parametrize(
    ("text", "days"),
    [
        ("2 weeks ago", 14),
        ("6 days ago", 6),
        ("3 months ago", 90),
        ("1 year ago", 365),
        ("Streamed 3 weeks ago", 21),
        ("Streamed a month ago", 30),  # parse_relative_date alone fails on "a" after a prefix
        ("Streamed live 2 days ago", 2),
        ("Premiered an hour ago", 0),
        ("6d ago", 6),
        ("2w ago", 14),
        ("5mo ago", 150),
        ("1y ago", 365),
        ("16y ago", 16 * 365),
        ("10m ago", 0),  # "m" is minutes, "mo" is months
        ("5h ago", 0),
        ("30s ago", 0),
        ("  3 weeks ago (edited) ", 21),
    ],
)
def test_card_date_parses_relative_texts(text: str, days: int) -> None:
    assert _card_date(text, TODAY) == _ago(days)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "17M", "1.2M views", "Scheduled for 12/12", "1.2K watching", "Streamed live"],
)
def test_card_date_is_none_for_non_dates(text: str) -> None:
    assert _card_date(text, TODAY) is None


def test_card_date_never_raises_on_absurd_counts() -> None:
    assert _card_date("999999999999999 years ago", TODAY) is None
    assert _card_date("99999999999 days ago", date(2000, 1, 1)) is None


# --------------------------------------------------------------------------- #
# Early stop
# --------------------------------------------------------------------------- #
def test_recent_stops_scrolling_once_the_last_card_is_older_than_since() -> None:
    # 400 cards, one per day, growing 6 per scroll: without the early stop this would
    # scroll ~66 times; with it, just until a card older than 30 d is rendered.
    drv = _GridDriver(_cards(list(range(1, 401))), page=6, grow=6)
    _, items = _run(drv, since=_ago(30))
    assert [it["video_id"] for it in items] == [_vid(a) for a in range(1, 31)]
    assert drv.scrolls == 5  # 6 + 5*6 = 36 cards rendered, the 31st is already too old
    assert drv.rendered < 60


def test_recent_early_stop_uses_the_last_dated_card() -> None:
    cards = _cards([1, 2, 3, 40]) + [(_vid(99), "")]  # undated tail card
    drv = _GridDriver(cards, page=5, grow=5)
    _, items = _run(drv, since=_ago(30))
    assert drv.scrolls == 0  # the 40 d card already proves everything further is older
    assert [it["video_id"] for it in items] == [_vid(1), _vid(2), _vid(3), _vid(99)]


def test_old_order_stops_once_the_last_card_is_newer_than_until() -> None:
    # Oldest first: ages 400 .. 1. Window = everything at least 300 d old.
    drv = _GridDriver(_cards(list(range(400, 0, -1))), page=10, grow=10)
    _, items = _run(drv, order="old", until=_ago(300))
    assert drv.chip_clicked is True
    assert [it["video_id"] for it in items] == [_vid(a) for a in range(400, 299, -1)]
    assert drv.scrolls == 10  # 110 rendered; 400 would take 39
    assert drv.rendered < 130


def test_old_order_with_both_bounds() -> None:
    drv = _GridDriver(_cards(list(range(400, 0, -1))), page=10, grow=10)
    _, items = _run(drv, order="old", since=_ago(390), until=_ago(380))
    assert [it["video_id"] for it in items] == [_vid(a) for a in range(390, 379, -1)]
    assert drv.scrolls <= 3


def test_popular_has_no_date_stop_it_only_filters() -> None:
    # Not date-sorted: with 2 cards rendered the last one (400 d) is already older than
    # `since`, which would end a `recent` scan right there and lose the later in-window cards.
    cards = _cards([3, 400, 5, 20, 50])
    drv = _GridDriver(cards, page=2, grow=2)
    _, items = _run(drv, order="popular", since=_ago(30))
    assert drv.chip_clicked is True
    assert [it["video_id"] for it in items] == [_vid(3), _vid(5), _vid(20)]


# --------------------------------------------------------------------------- #
# limit semantics
# --------------------------------------------------------------------------- #
def test_limit_caps_after_filtering_not_before() -> None:
    # Cards aged 1..9 are newer than `until` and filtered out; a pre-filter cap of 5
    # would return nothing. The first FIVE IN-WINDOW cards are 10..14.
    drv = _GridDriver(_cards(list(range(1, 41))), page=6, grow=6)
    _, items = _run(drv, limit=5, since=_ago(30), until=_ago(10))
    assert [it["video_id"] for it in items] == [_vid(a) for a in range(10, 15)]
    assert drv.scrolls <= 3  # stops at the 5th in-window card, not at the window's far edge


def test_limit_zero_with_a_window_means_no_count_limit() -> None:
    drv = _GridDriver(_cards(list(range(1, 101))), page=10, grow=10)
    name, items = _run(drv, limit=0, since=_ago(30))
    assert name == "Someone"
    assert len(items) == 30


def test_negative_limit_with_a_window_also_means_no_count_limit() -> None:
    _, items = _run(_GridDriver(_cards(list(range(1, 41)))), limit=-1, since=_ago(10))
    assert len(items) == 10


def test_limit_larger_than_the_window_returns_the_whole_window() -> None:
    _, items = _run(_GridDriver(_cards(list(range(1, 41)))), limit=500, since=_ago(10))
    assert len(items) == 10


# --------------------------------------------------------------------------- #
# Runaway guards and failure modes
# --------------------------------------------------------------------------- #
def test_step_budget_bounds_a_grid_that_never_settles(monkeypatch) -> None:
    monkeypatch.setattr(scraper, "_CHANNEL_WINDOW_MAX_STEPS", 7)
    # Every card is 1 d old, so a `since` of 5 d ago is never undercut and the grid keeps
    # growing: only the budget can end this.
    drv = _GridDriver([(_vid(i), "1 day ago") for i in range(500)], page=1, grow=1)
    _, items = _run(drv, since=_ago(5))
    assert drv.scrolls == 7
    assert len(items) == 8


def test_stagnant_grid_stops_after_the_stale_tolerance() -> None:
    drv = _GridDriver(_cards([1, 2, 3, 4]), page=4, grow=0)
    _, items = _run(drv, since=_ago(5))
    assert drv.scrolls == scraper._CHANNEL_WINDOW_STALE
    assert len(items) == 4


@pytest.mark.parametrize("raise_after", [0, 1, 2, 3, 4, 5, 6, 8, 12])
def test_driver_failing_midway_never_raises(raise_after: int) -> None:
    drv = _GridDriver(_cards(list(range(1, 100))), raise_after=raise_after)
    name, items = _run(drv, since=_ago(60))
    assert isinstance(name, str)
    assert isinstance(items, list)
    if raise_after < 4:  # died before the harvest could run: the plain failure result
        assert (name, items) == ("", [])


def test_driver_failing_on_navigation_returns_the_plain_failure_result() -> None:
    class _Boom(_GridDriver):
        def get(self, url: str) -> None:
            raise WebDriverException("boom")

    assert _run(_Boom(_cards([1, 2])), since=_ago(5)) == ("", [])


def test_garbage_probe_and_harvest_payloads_do_not_raise() -> None:
    drv = _GridDriver(_cards([1, 2, 3]), probe_result="not-a-list")
    name, items = _run(drv, since=_ago(5))
    assert (name, items) == ("Someone", [])
    assert drv.scrolls == scraper._CHANNEL_WINDOW_STALE  # a dateless probe can't prove a stop


# --------------------------------------------------------------------------- #
# Selector hygiene
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
