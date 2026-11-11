"""CLI behaviour of the channel time-window product (``--since/--until/--dates/--scan-cap``).

No browser, no network: the engines (``ytdl.channel_entries_window`` / ``fill_dates`` /
``channel_entries``, ``scraper.collect_channel_videos``, the driver builder) are patched on
the CONSUMER module ``viewlyt.cli``, exactly like the plain ``-C`` tests in
tests/test_integration.py, and "today" is pinned through ``cli._today_utc``.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from conftest import FakeDriver

import viewlyt.cli as cli
from viewlyt.htmltext import format_related

TODAY = date(2026, 7, 23)
SINCE = date(2026, 1, 1)
_REAL_TODAY_UTC = cli._today_utc  # captured before the autouse pin replaces it


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _items(n: int, prefix: str = "v", *, dated: bool = False) -> list[dict]:
    """``n`` channel items (the ``_entry_to_item`` shape); ``dated`` adds a "date"."""
    out = []
    for i in range(1, n + 1):
        item = {
            "video_id": f"{prefix}{i}",
            "title": f"Video {prefix}{i}",
            "views": f"{i * 1000:,} views",
            "url": f"https://www.youtube.com/watch?v={prefix}{i}",
        }
        if dated:
            item["date"] = f"2026-03-{i:02d}"
        out.append(item)
    return out


def _meta(**over) -> dict:
    return {
        "probes": 8,
        "flat_requests": 3,
        "scanned": 64,
        "truncated_by_cap": False,
        "non_monotonic": False,
    } | over


class Engines:
    """Recorders + canned results for every I/O boundary the channel mode touches.

    ``window``/``selenium``/``plain`` hold either a result, an exception to raise, or a
    ``callable(ref)`` (``plain`` takes ``(ref, n)``) so one test can script per-channel
    behaviour."""

    def __init__(self, monkeypatch) -> None:
        self.window_calls: list[dict] = []
        self.plain_calls: list[tuple] = []
        self.fill_calls: list[list[dict]] = []
        self.selenium_calls: list[dict] = []
        self.built: list[FakeDriver] = []
        self.known_dates: dict[str, str] = {}  # what fill_dates "learns", by video id
        self.window = ("Veritasium", _items(3), _meta())
        self.selenium = ("Veritasium", _items(2, "s", dated=True))
        self.plain = ("Veritasium", _items(3))
        monkeypatch.setattr(cli.ytdl, "channel_entries_window", self._window)
        monkeypatch.setattr(cli.ytdl, "channel_entries", self._plain)
        monkeypatch.setattr(cli.ytdl, "fill_dates", self._fill)
        monkeypatch.setattr(cli, "collect_channel_videos", self._selenium)
        monkeypatch.setattr(cli, "build_primed_driver", self._build)

    @staticmethod
    def _resolve(value, *args):
        if isinstance(value, Exception):
            raise value
        return value(*args) if callable(value) else value

    def _window(self, ref, since, until, *, order="recent", cap=5000):
        self.window_calls.append(
            {"ref": ref, "since": since, "until": until, "order": order, "cap": cap}
        )
        return self._resolve(self.window, ref)

    def _plain(self, ref, n, order="recent"):
        self.plain_calls.append((ref, n, order))
        return self._resolve(self.plain, ref, n)

    def _fill(self, items, *, workers=4):
        self.fill_calls.append(items)
        filled = 0
        for it in items:
            if not it.get("date") and it["video_id"] in self.known_dates:
                it["date"] = self.known_dates[it["video_id"]]
                filled += 1
        return filled

    def _selenium(
        self, driver, ref, limit, order="recent", progress=True, *, since=None, until=None
    ):
        self.selenium_calls.append(
            {"ref": ref, "limit": limit, "order": order, "since": since, "until": until}
        )
        return self._resolve(self.selenium, ref)

    def _build(self, headless, user_data_dir):
        drv = FakeDriver()
        self.built.append(drv)
        return drv

    @property
    def untouched(self) -> bool:
        """True when no engine was called at all (the usage-error contract)."""
        return not (
            self.window_calls
            or self.plain_calls
            or self.fill_calls
            or self.selenium_calls
            or self.built
        )


@pytest.fixture(autouse=True)
def _pin_today(monkeypatch):
    monkeypatch.setattr(cli, "_today_utc", lambda: TODAY)


@pytest.fixture
def eng(monkeypatch):
    return Engines(monkeypatch)


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _batch(tmp_path, refs=("@veritasium",), **over):
    """``run_channel_batch`` in window mode with sensible defaults."""
    kw = dict(
        n=0,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
        since=SINCE,
        until=TODAY,
    )
    kw.update(over)
    return cli.run_channel_batch(list(refs), **kw)


# --------------------------------------------------------------------------- #
# Routing and argument passing
# --------------------------------------------------------------------------- #
def test_since_alone_routes_to_channel_mode_and_writes_listing(eng, tmp_path, capsys):
    rc = cli.main(["--since", "2026-01-01", "-o", str(tmp_path), "@veritasium"])
    assert rc == 0
    # until defaults to today; order/cap defaults travel to the engine.
    assert eng.window_calls == [
        {
            "ref": "@veritasium",
            "since": SINCE,
            "until": TODAY,
            "order": "recent",
            "cap": 5000,
        }
    ]
    assert eng.plain_calls == [] and eng.selenium_calls == [] and eng.built == []
    out_file = tmp_path / "veritasium.channel.md"
    assert out_file.read_text(encoding="utf-8") == "\n".join(format_related(_items(3))) + "\n"
    out = capsys.readouterr().out
    assert "Done: 1/1 channel(s) listed" in out


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("90d", TODAY - timedelta(days=90)),
        ("12w", TODAY - timedelta(weeks=12)),
        ("6m", TODAY - timedelta(days=180)),
        ("1y", TODAY - timedelta(days=365)),
        ("0d", TODAY),
        ("2025-02-28", date(2025, 2, 28)),
    ],
)
def test_since_specs_are_parsed_against_pinned_today(eng, tmp_path, spec, expected):
    assert cli.main(["--since", spec, "-o", str(tmp_path), "@veritasium"]) == 0
    call = eng.window_calls[0]
    assert call["since"] == expected
    assert call["until"] == TODAY


@pytest.mark.parametrize(
    ("until", "expected"),
    [("2026-06-30", date(2026, 6, 30)), ("30d", TODAY - timedelta(days=30))],
)
def test_until_is_parsed_and_forwarded(eng, tmp_path, until, expected):
    argv = ["--since", "2026-01-01", "--until", until, "-o", str(tmp_path), "@veritasium"]
    assert cli.main(argv) == 0
    assert eng.window_calls[0]["until"] == expected
    assert eng.window_calls[0]["since"] == SINCE


def test_since_equal_until_is_a_valid_one_day_window(eng, tmp_path):
    argv = ["--since", "2026-03-05", "--until", "2026-03-05", "-o", str(tmp_path), "@veritasium"]
    assert cli.main(argv) == 0
    assert eng.window_calls[0]["since"] == eng.window_calls[0]["until"] == date(2026, 3, 5)


def test_order_and_scan_cap_are_forwarded(eng, tmp_path):
    argv = ["--since", "6m", "--order", "old", "--scan-cap", "250", "-o", str(tmp_path), "@xyz"]
    assert cli.main(argv) == 0
    assert eng.window_calls[0]["order"] == "old"
    assert eng.window_calls[0]["cap"] == 250


def test_scan_cap_zero_means_unlimited_and_is_forwarded_verbatim(eng, tmp_path, caplog):
    assert cli.main(["--since", "6m", "--scan-cap", "0", "-o", str(tmp_path), "@xyz"]) == 0
    assert eng.window_calls[0]["cap"] == 0
    assert not [w for w in _warnings(caplog) if "--scan-cap" in w]


def test_window_flags_stay_raw_strings_in_argparse():
    # Parsing happens in main (one `today`, errors via log.error + exit 2), so a
    # bad spec must NOT make argparse itself exit.
    ns = cli.build_parser().parse_args(["--since", "banana", "--until", "soon", "@x"])
    assert (ns.since, ns.until) == ("banana", "soon")
    assert ns.dates is False and ns.scan_cap == 5000


def test_help_documents_the_window_flags():
    text = cli.build_parser().format_help()
    for token in ("--since", "--until", "--dates", "--scan-cap"):
        assert token in text


def test_today_utc_is_the_utc_calendar_day():
    before = datetime.now(UTC).date()
    got = _REAL_TODAY_UTC()
    after = datetime.now(UTC).date()
    assert before <= got <= after


def test_run_channel_batch_defaults_until_to_today(eng, tmp_path):
    s = _batch(tmp_path, until=None)[0]
    assert eng.window_calls[0]["until"] == TODAY
    assert s["until"] == TODAY.isoformat()


def test_channel_mode_logs_the_window(eng, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cli.main(["-C", "7", "--since", "2026-01-01", "-o", str(tmp_path), "@veritasium"])
    assert any("window=2026-01-01..2026-07-23" in r.getMessage() for r in caplog.records)
    assert any("n=7" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# -C alone is untouched; -C + window is a ceiling
# --------------------------------------------------------------------------- #
def test_c_alone_still_uses_channel_entries_never_the_window_function(eng, tmp_path, capsys):
    assert cli.main(["-C", "3", "-o", str(tmp_path), "@veritasium"]) == 0
    assert eng.plain_calls == [("@veritasium", 3, "recent")]
    assert eng.window_calls == []
    out = capsys.readouterr().out
    # the plain line: no range, no probes, no window note
    assert "3 videos (recent, yt-dlp) -> " in out
    assert "probes" not in out and "2026-" not in out


def test_plain_summary_has_no_window_keys_but_carries_the_listed_items(eng, tmp_path):
    s = cli.run_channel_batch(
        ["@veritasium"],
        n=3,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )[0]
    # "items" (what was listed) feeds the channel -> per-video pipeline; no window keys.
    assert set(s) == {
        "ref",
        "channel",
        "count",
        "order",
        "engine",
        "channel_file",
        "error",
        "items",
    }
    assert s["items"] == _items(3)


def test_c_ceiling_cuts_the_windowed_list_to_n(eng, tmp_path):
    eng.window = ("Veritasium", _items(5), _meta())
    assert cli.main(["-C", "3", "--since", "1y", "-o", str(tmp_path), "@veritasium"]) == 0
    lines = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8").splitlines()
    assert lines == format_related(_items(3))  # the first 3, in order
    assert eng.plain_calls == []


def test_ceiling_larger_than_the_window_keeps_everything(eng, tmp_path):
    eng.window = ("Veritasium", _items(2), _meta())
    s = _batch(tmp_path, n=50)[0]
    assert s["count"] == 2


def test_ceiling_applies_after_the_dates_refilter(eng, tmp_path):
    # [a, out, b, c] -> re-filter drops `out` -> [a, b, c] -> cut to 2 -> [a, b].
    # Cutting BEFORE the re-filter would leave [a] and lose a real in-window video.
    items = _items(4)
    eng.window = ("Veritasium", items, _meta())
    eng.known_dates = {
        "v1": "2026-02-01",
        "v2": "2025-12-31",
        "v3": "2026-02-02",
        "v4": "2026-02-03",
    }
    s = _batch(tmp_path, n=2, dates=True)[0]
    assert s["count"] == 2
    text = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8")
    assert "Video v1" in text and "Video v3" in text and "Video v2" not in text


def test_ceiling_with_order_old_keeps_the_oldest(eng, tmp_path):
    # ytdl reverses the list for order="old"; the ceiling just takes the head of it.
    eng.window = ("Veritasium", list(reversed(_items(4))), _meta())
    _batch(tmp_path, n=2, order="old")
    text = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8")
    assert "Video v4" in text and "Video v3" in text and "Video v1" not in text


# --------------------------------------------------------------------------- #
# Usage errors (exit 2, message names the flag, engines never touched)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "fragments"),
    [
        (["--until", "2026-01-01"], ["--until", "--since"]),
        (["--since", "banana"], ["--since", "banana"]),
        (["--since", "90"], ["--since", "yyyy-mm-dd"]),
        (["--since", "2026-02-30"], ["--since", "invalid date"]),
        (["--since", ""], ["--since"]),
        (["--since", "90d", "--until", "soon"], ["--until", "soon"]),
        (["--since", "2026-07-01", "--until", "2026-06-01"], ["--since", "--until", "empty"]),
        (["--since", "2027-01-01"], ["--since", "2027-01-01", "2026-07-23"]),
        (["--since", "30d", "--until", "90d"], ["--since", "--until"]),
        (["--since", "90d", "--order", "popular"], ["--order popular", "--since"]),
        (["--since", "90d", "--scan-cap", "-1"], ["--scan-cap"]),
        (["-C", "5", "--scan-cap", "-1"], ["--scan-cap"]),
    ],
)
def test_usage_errors_exit_two_with_a_flag_naming_message(
    eng, tmp_path, caplog, monkeypatch, argv, fragments
):
    def boom(*a, **k):
        raise AssertionError("no scrape may start on a usage error")

    monkeypatch.setattr(cli, "run_batch", boom)
    rc = cli.main([*argv, "-o", str(tmp_path), "@veritasium"])
    assert rc == 2
    errors = _errors(caplog)
    assert len(errors) == 1, errors
    for frag in fragments:
        assert frag in errors[0], (frag, errors[0])
    assert eng.untouched
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "selector",
    [["-c"], ["-t"], ["--transcript-only"], ["-r", "5"], ["-u"], ["--unify-all"]],
)
@pytest.mark.parametrize(
    "listing", [["-C", "5"], ["--since", "90d"], ["-C", "5", "--since", "90d"]]
)
def test_a_selector_beside_c_or_since_is_a_pipeline_not_a_usage_error(
    eng, tmp_path, caplog, monkeypatch, listing, selector
):
    """Was: exit 2 "... incompatible with -c/-t/-r/-u/--unify-all". Now the listing runs
    (same engines, same ``.channel.md``) and its videos are handed to ``run_batch``."""
    ids = ["abcdefghi01", "abcdefghi02"]
    items = [
        {"video_id": v, "title": v, "views": "", "url": f"https://www.youtube.com/watch?v={v}"}
        for v in ids
    ]
    eng.window = ("Veritasium", items, _meta())
    eng.plain = ("Veritasium", items)
    calls = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(list(targets)) or [])

    rc = cli.main([*listing, *selector, "-o", str(tmp_path), "@veritasium"])
    assert rc == 0
    assert _errors(caplog) == []
    assert (tmp_path / "veritasium.channel.md").exists()
    assert calls == [[(v, f"https://www.youtube.com/watch?v={v}") for v in ids]]
    windowed = "--since" in listing
    assert bool(eng.window_calls) is windowed and bool(eng.plain_calls) is not windowed


def test_window_with_no_valid_channel_ref_exits_two(eng, tmp_path, caplog):
    assert cli.main(["--since", "90d", "-o", str(tmp_path), "not a channel ref"]) == 2
    assert any("no valid channel" in e for e in _errors(caplog))
    assert eng.untouched


@pytest.mark.parametrize(
    ("argv", "flag"),
    [(["--dates"], "--dates"), (["--scan-cap", "100"], "--scan-cap")],
)
def test_window_only_flags_without_a_window_warn_and_do_nothing(
    eng, tmp_path, caplog, monkeypatch, argv, flag
):
    called = {}

    def fake_run_batch(targets, **kw):
        called["targets"] = targets
        return [
            {
                "video_id": "dQw4w9WgXcQ",
                "error": None,
                "with_comments": False,
                "file": None,
                "comments": 0,
                "lines": 0,
                "with_transcript": True,
                "transcript_file": "tf",
                "segments": 1,
            }
        ]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    assert cli.main([*argv, "-o", str(tmp_path), "dQw4w9WgXcQ"]) == 0
    assert called  # normal video scrape went ahead
    assert any(flag in w and "--since" in w for w in _warnings(caplog))
    assert eng.untouched


@pytest.mark.parametrize(
    ("argv", "flag"), [(["--dates"], "--dates"), (["--scan-cap", "9"], "--scan-cap")]
)
def test_window_only_flags_warn_in_plain_channel_mode_too(eng, tmp_path, caplog, argv, flag):
    assert cli.main(["-C", "3", *argv, "-o", str(tmp_path), "@veritasium"]) == 0
    assert any(flag in w and "--since" in w for w in _warnings(caplog))
    assert eng.fill_calls == [] and eng.window_calls == []
    assert eng.plain_calls == [("@veritasium", 3, "recent")]


def test_window_only_flags_do_not_warn_when_a_window_is_given(eng, tmp_path, caplog):
    cli.main(["--since", "90d", "--dates", "--scan-cap", "10", "-o", str(tmp_path), "@xyz"])
    assert not [w for w in _warnings(caplog) if "has no effect" in w]


def test_order_warning_mentions_since_and_is_still_a_no_op(eng, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: [])
    cli.main(["--order", "old", "-o", str(tmp_path), "dQw4w9WgXcQ"])
    assert any(
        "--order has no effect without -C/--channel or --since" in w for w in _warnings(caplog)
    )


# --------------------------------------------------------------------------- #
# Genuinely empty window vs. engine failure
# --------------------------------------------------------------------------- #
def test_genuinely_empty_window_is_a_success_with_no_file_and_no_fallback(eng, tmp_path, capsys):
    eng.window = ("Veritasium", [], _meta(scanned=64))
    rc = cli.main(["--since", "2026-01-01", "-o", str(tmp_path), "@veritasium"])
    assert rc == 0
    assert list(tmp_path.iterdir()) == []
    assert eng.selenium_calls == [] and eng.built == []
    out, err = capsys.readouterr()
    assert "Done: 1/1 channel(s) listed" in out
    assert "failed" not in out and "✗" not in out + err
    assert (
        "Veritasium  0 videos (2026-01-01..2026-07-23, recent, yt-dlp, 8 probes) "
        "(no videos in the window)"
    ) in out
    assert "->" not in out


def test_genuinely_empty_window_summary_fields(eng, tmp_path):
    eng.window = ("Veritasium", [], _meta(scanned=5))
    s = _batch(tmp_path)[0]
    assert (s["count"], s["channel_file"], s["error"], s["engine"]) == (0, None, None, "yt-dlp")


def test_empty_window_still_honours_no_fallback(eng, tmp_path):
    eng.window = ("Veritasium", [], _meta(scanned=64))
    s = _batch(tmp_path, fallback=False)[0]
    assert s["error"] is None and s["count"] == 0
    assert eng.built == []


def test_empty_window_does_not_copy(eng, tmp_path, monkeypatch):
    eng.window = ("Veritasium", [], _meta(scanned=64))
    monkeypatch.setattr(cli, "_copy_to_clipboard", lambda text: pytest.fail("nothing to copy"))
    _batch(tmp_path, copy=True)


def test_every_channel_empty_still_exits_zero(eng, tmp_path):
    eng.window = ("Chan", [], _meta(scanned=10))
    argv = ["--since", "30d", "-o", str(tmp_path), "@aaa", "@bbb"]
    assert cli.main(argv) == 0


def test_dates_refilter_that_empties_the_window_is_a_success_not_a_fallback(eng, tmp_path):
    eng.window = ("Veritasium", _items(2), _meta())
    eng.known_dates = {"v1": "2025-01-01", "v2": "2025-01-02"}  # both before SINCE
    s = _batch(tmp_path, dates=True)[0]
    assert (s["count"], s["error"], s["channel_file"]) == (0, None, None)
    assert eng.selenium_calls == [] and eng.built == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("n", [0, 7])
def test_empty_tab_falls_back_to_selenium_with_window_and_limit(eng, tmp_path, n):
    eng.window = ("", [], _meta(scanned=0, probes=0))
    s = _batch(tmp_path, n=n)[0]
    assert eng.selenium_calls == [
        {"ref": "@veritasium", "limit": n, "order": "recent", "since": SINCE, "until": TODAY}
    ]
    assert s["engine"] == "selenium" and s["error"] is None and s["count"] == 2
    assert (tmp_path / "veritasium.channel.md").exists()


def test_main_fallback_passes_limit_zero_without_c_and_n_with_c(eng, tmp_path):
    eng.window = ("", [], _meta(scanned=0))
    cli.main(["--since", "1y", "-o", str(tmp_path), "@veritasium"])
    cli.main(["-C", "9", "--since", "1y", "-o", str(tmp_path), "@veritasium"])
    assert [c["limit"] for c in eng.selenium_calls] == [0, 9]
    assert all(
        c["since"] == TODAY - timedelta(days=365) and c["until"] == TODAY
        for c in eng.selenium_calls
    )


@pytest.mark.parametrize("exc", [RuntimeError("yt-dlp boom"), NotImplementedError("popular")])
def test_ytdlp_raising_falls_back_to_selenium(eng, tmp_path, exc):
    eng.window = exc
    s = _batch(tmp_path)[0]
    assert s["engine"] == "selenium" and s["error"] is None
    assert eng.selenium_calls[0]["since"] == SINCE
    assert eng.fill_calls == []


def test_fallback_also_empty_is_an_error_and_exit_one(eng, tmp_path, capsys):
    eng.window = RuntimeError("boom")
    eng.selenium = ("", [])
    rc = cli.main(["--since", "2026-01-01", "-o", str(tmp_path), "@nobody"])
    assert rc == 1
    assert list(tmp_path.iterdir()) == []
    out, err = capsys.readouterr()
    assert "0/1 channel(s) listed, 1 failed" in out
    assert "yt-dlp and Selenium both failed" in err


def test_no_fallback_with_an_engine_failure_is_an_error_without_a_driver(eng, tmp_path, capsys):
    eng.window = ("", [], _meta(scanned=0))
    rc = cli.main(["--since", "2026-01-01", "--no-fallback", "-o", str(tmp_path), "@veritasium"])
    assert rc == 1
    assert eng.built == [] and eng.selenium_calls == []
    assert "Selenium fallback is disabled" in capsys.readouterr().err


def test_no_fallback_error_names_the_ytdlp_exception_when_there_was_one(eng, tmp_path):
    eng.window = RuntimeError("gave up after 200 date probes (limit 200)")
    s = _batch(tmp_path, fallback=False)[0]
    assert s["error"] == (
        "yt-dlp failed (gave up after 200 date probes (limit 200)) "
        "and the Selenium fallback is disabled"
    )
    assert s["channel_file"] is None and eng.built == []


def test_no_fallback_error_for_an_empty_tab_keeps_the_original_text(eng, tmp_path):
    eng.window = ("", [], _meta(scanned=0))  # yt-dlp answered, with nothing: no exception
    s = _batch(tmp_path, fallback=False)[0]
    assert s["error"] == "yt-dlp returned no videos and the Selenium fallback is disabled"


def test_plain_no_fallback_error_names_the_ytdlp_exception_too(eng, tmp_path):
    eng.plain = RuntimeError("HTTP Error 404")
    s = cli.run_channel_batch(
        ["@veritasium"],
        n=3,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=False,
        user_data_dir=None,
        quiet=True,
    )[0]
    assert s["error"] == "yt-dlp failed (HTTP Error 404) and the Selenium fallback is disabled"


def _lost_listing(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "yt-dlp channel listing failed" in r.getMessage()]


def test_window_listing_failure_is_a_warning_that_survives_quiet(eng, tmp_path, caplog):
    # No caplog.set_level: the root logger stays at its default WARNING, which is exactly
    # what `-q` configures. An INFO record would be dropped here; a WARNING is kept.
    eng.window = RuntimeError("gave up after 200 date probes (limit 200)")
    rc = cli.main(["-q", "--since", "90d", "-o", str(tmp_path), "@veritasium"])
    assert rc == 0
    (record,) = _lost_listing(caplog)
    assert record.levelno == logging.WARNING
    msg = record.getMessage()
    assert "@veritasium" in msg and "gave up after 200 date probes" in msg
    assert msg.endswith("falling back to Selenium")


def test_window_listing_failure_warning_without_the_fallback_does_not_promise_one(
    eng, tmp_path, caplog
):
    eng.window = RuntimeError("boom")
    _batch(tmp_path, fallback=False)
    (record,) = _lost_listing(caplog)
    assert record.levelno == logging.WARNING
    assert "falling back" not in record.getMessage() and "boom" in record.getMessage()


def test_plain_listing_failure_stays_at_info(eng, tmp_path, caplog):
    # Plain -C falls back routinely (`popular` always does): INFO, so -q hides it.
    eng.plain = RuntimeError("HTTP Error 404")
    caplog.set_level(logging.INFO)
    cli.main(["-C", "3", "-o", str(tmp_path), "@veritasium"])
    (record,) = _lost_listing(caplog)
    assert record.levelno == logging.INFO
    assert "HTTP Error 404" in record.getMessage()
    assert record.getMessage().endswith("falling back to Selenium")


def test_plain_listing_failure_is_invisible_under_quiet(eng, tmp_path, caplog):
    eng.plain = RuntimeError("HTTP Error 404")
    cli.main(["-q", "-C", "3", "-o", str(tmp_path), "@veritasium"])  # default WARNING level
    assert _lost_listing(caplog) == []


def test_selenium_raising_is_isolated_and_recycles_the_driver(eng, tmp_path):
    eng.window = ("", [], _meta(scanned=0))

    def selenium(ref):
        if ref == "@a":
            raise RuntimeError("session died")
        return "B", _items(1, "b", dated=True)

    eng.selenium = selenium
    a, b = _batch(tmp_path, refs=("@a", "@b"))
    assert a["error"] == "session died" and a["channel_file"] is None
    assert b["error"] is None and b["engine"] == "selenium"
    assert len(eng.built) == 2 and eng.built[0].quit_calls == 1 and eng.built[1].quit_calls == 1


# --------------------------------------------------------------------------- #
# --dates
# --------------------------------------------------------------------------- #
def test_dates_fills_refilters_and_keeps_undated(eng, tmp_path):
    items = _items(5)
    items[0]["date"] = "2026-02-10"  # already dated by a probe, in the window
    eng.window = ("Veritasium", items, _meta())
    eng.known_dates = {
        "v2": "2026-03-01",
        "v3": "2025-12-31",  # one day BEFORE since: a bisection miss, must go
        "v4": "2026-07-24",  # one day AFTER until: must go
        # v5 stays undated (private/removed): kept
    }
    s = _batch(tmp_path, dates=True)[0]
    assert len(eng.fill_calls) == 1
    assert s["count"] == 3
    lines = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8").splitlines()
    assert lines == [
        "1. [1,000 views. 2026-02-10. Video v1](https://www.youtube.com/watch?v=v1)",
        "2. [2,000 views. 2026-03-01. Video v2](https://www.youtube.com/watch?v=v2)",
        "3. [5,000 views. Video v5](https://www.youtube.com/watch?v=v5)",
    ]


def test_dates_keeps_both_inclusive_edges(eng, tmp_path):
    eng.window = ("Veritasium", _items(2), _meta())
    eng.known_dates = {"v1": SINCE.isoformat(), "v2": TODAY.isoformat()}
    assert _batch(tmp_path, dates=True)[0]["count"] == 2


def test_dates_keeps_an_unparseable_date_instead_of_dropping_it(eng, tmp_path):
    items = _items(1)
    items[0]["date"] = "sometime in March"
    eng.window = ("Veritasium", items, _meta())
    assert _batch(tmp_path, dates=True)[0]["count"] == 1


def test_without_dates_fill_dates_is_not_called_and_probe_dates_survive(eng, tmp_path):
    items = _items(3)
    items[0]["date"] = "2026-02-10"
    eng.window = ("Veritasium", items, _meta())
    s = _batch(tmp_path)[0]
    assert eng.fill_calls == [] and s["count"] == 3
    lines = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("1. [1,000 views. 2026-02-10. Video v1]")
    assert lines[1].startswith("2. [2,000 views. Video v2]")  # undated stays undated


def test_dates_flag_from_main_reaches_fill_dates(eng, tmp_path):
    eng.known_dates = {"v1": "2026-02-01", "v2": "2026-02-02", "v3": "2026-02-03"}
    assert cli.main(["--since", "2026-01-01", "--dates", "-o", str(tmp_path), "@veritasium"]) == 0
    assert len(eng.fill_calls) == 1
    text = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8")
    assert all(d in text for d in ("2026-02-01", "2026-02-02", "2026-02-03"))


def test_dates_is_not_called_on_the_selenium_engine_and_its_dates_are_shown(eng, tmp_path):
    eng.window = ("", [], _meta(scanned=0))
    s = _batch(tmp_path, dates=True)[0]
    assert s["engine"] == "selenium"
    assert eng.fill_calls == []
    text = (tmp_path / "veritasium.channel.md").read_text(encoding="utf-8")
    assert "2026-03-01" in text and "2026-03-02" in text  # the engine's own approx dates


def test_dates_with_no_items_does_not_call_fill_dates(eng, tmp_path):
    eng.window = ("Veritasium", [], _meta(scanned=3))
    _batch(tmp_path, dates=True)
    assert eng.fill_calls == []


# --------------------------------------------------------------------------- #
# Summary shape, the ✓ line, the warnings
# --------------------------------------------------------------------------- #
def test_summary_keys_on_the_ytdlp_engine(eng, tmp_path):
    eng.window = ("Veritasium", _items(3), _meta(probes=11))
    s = _batch(tmp_path)[0]
    assert s["since"] == "2026-01-01" and s["until"] == "2026-07-23"
    assert s["probes"] == 11
    assert s["truncated_by_cap"] is False and s["non_monotonic"] is False
    assert s["approx_dates"] is False
    assert s["order"] == "recent" and s["engine"] == "yt-dlp"


def test_summary_keys_on_the_selenium_engine(eng, tmp_path):
    eng.window = RuntimeError("boom")
    s = _batch(tmp_path)[0]
    assert s["engine"] == "selenium" and s["approx_dates"] is True
    assert s["probes"] is None
    assert s["truncated_by_cap"] is False and s["non_monotonic"] is False


def test_the_check_line_shows_range_and_probes_on_ytdlp(eng, tmp_path, capsys):
    eng.window = ("Veritasium", _items(23), _meta(probes=8))
    assert cli.main(["--since", "2026-01-01", "-o", str(tmp_path), "@veritasium"]) == 0
    out = capsys.readouterr().out
    assert (
        "  ✓ Veritasium  23 videos (2026-01-01..2026-07-23, recent, yt-dlp, 8 probes) "
        f"-> {tmp_path / 'veritasium.channel.md'}"
    ) in out


def test_the_check_line_has_no_probes_on_selenium(eng, tmp_path, capsys):
    eng.window = RuntimeError("boom")
    cli.main(["--since", "2026-01-01", "--order", "old", "-o", str(tmp_path), "@veritasium"])
    out = capsys.readouterr().out
    assert (
        "  ✓ Veritasium  2 videos (2026-01-01..2026-07-23, old, selenium) "
        f"-> {tmp_path / 'veritasium.channel.md'}"
    ) in out
    assert "probes" not in out


def test_multi_channel_progress_line_matches_the_final_report_line(eng, tmp_path, capsys):
    eng.window = lambda ref: (ref.lstrip("@").title(), _items(2), _meta(probes=4))
    sums = _batch(tmp_path, refs=("@alpha", "@beta"), quiet=False)
    assert [s["channel_file"] is not None for s in sums] == [True, True]
    out = capsys.readouterr().out
    for s in sums:
        line = cli._channel_line(s)
        assert f"✓ {line}" in out
        assert "(2026-01-01..2026-07-23, recent, yt-dlp, 4 probes)" in line


def test_multi_channel_progress_line_for_an_empty_window(eng, tmp_path, capsys):
    eng.window = ("Chan", [], _meta(scanned=9, probes=2))
    _batch(tmp_path, refs=("@alpha", "@beta"), quiet=False)
    out = capsys.readouterr().out
    assert (
        "Chan  0 videos (2026-01-01..2026-07-23, recent, yt-dlp, 2 probes) (no videos in the window)"
        in out
    )


def test_truncated_by_cap_warns_with_channel_cap_and_flag(eng, tmp_path, caplog):
    eng.window = ("Veritasium", _items(2), _meta(truncated_by_cap=True))
    s = _batch(tmp_path, scan_cap=1234)[0]
    assert s["truncated_by_cap"] is True
    msgs = [w for w in _warnings(caplog) if "incomplete" in w]
    assert len(msgs) == 1
    assert "Veritasium" in msgs[0] and "1234" in msgs[0] and "--scan-cap" in msgs[0]


@pytest.mark.parametrize("dates", [False, True])
def test_non_monotonic_warns_without_overselling_dates(eng, tmp_path, caplog, dates):
    """The old text promised "rerun with --dates for exact edges". --dates only re-filters what
    the search RETURNED: it drops extras, it can't bring back a video the search skipped. The
    message says so, the same way whether or not --dates was already used."""
    eng.window = ("Veritasium", _items(2), _meta(non_monotonic=True))
    eng.known_dates = {"v1": "2026-02-01", "v2": "2026-02-02"}
    s = _batch(tmp_path, dates=dates)[0]
    assert s["non_monotonic"] is True
    msgs = [w for w in _warnings(caplog) if "out-of-order" in w]
    assert len(msgs) == 1
    msg = msgs[0]
    assert "Veritasium" in msg and "edges may be off" in msg
    assert "drops extras" in msg and "can't recover" in msg and "widen the window" in msg
    assert "rerun" not in msg and "exact edges" not in msg


def test_selenium_engine_warns_that_dates_are_approximate(eng, tmp_path, caplog):
    eng.window = RuntimeError("boom")
    _batch(tmp_path)
    msgs = [w for w in _warnings(caplog) if "approximate" in w]
    assert len(msgs) == 1 and "Veritasium" in msgs[0] and "Selenium" in msgs[0]


def test_selenium_failure_does_not_emit_the_approximate_warning(eng, tmp_path, caplog):
    eng.window = RuntimeError("boom")
    eng.selenium = ("", [])
    _batch(tmp_path)
    assert not [w for w in _warnings(caplog) if "approximate" in w]


def test_no_caveat_warnings_on_a_clean_ytdlp_run(eng, tmp_path, caplog):
    _batch(tmp_path)
    assert not [
        w
        for w in _warnings(caplog)
        if any(k in w for k in ("incomplete", "out-of-order", "approximate"))
    ]


def test_caveat_warnings_survive_quiet_mode(eng, tmp_path, caplog):
    eng.window = ("Veritasium", _items(1), _meta(truncated_by_cap=True))
    cli.main(["-q", "--since", "30d", "-o", str(tmp_path), "@veritasium"])
    assert any("incomplete" in w for w in _warnings(caplog))


# --------------------------------------------------------------------------- #
# Multiple channels, shared driver, --copy
# --------------------------------------------------------------------------- #
def test_one_failing_channel_does_not_sink_the_others(eng, tmp_path, capsys):
    def window(ref):
        if ref == "@bad":
            return "", [], _meta(scanned=0)
        return "Good", _items(2), _meta()

    eng.window = window
    eng.selenium = ("", [])  # the fallback finds nothing for @bad
    rc = cli.main(["--since", "90d", "-o", str(tmp_path), "@bad", "@good"])
    assert rc == 0
    assert (tmp_path / "good.channel.md").exists()
    out, err = capsys.readouterr()
    assert "1/2 channel(s) listed, 1 failed" in out
    assert "@bad" in err


def test_the_selenium_driver_is_built_lazily_once_and_shared(eng, tmp_path):
    eng.window = ("", [], _meta(scanned=0))
    eng.selenium = lambda ref: (ref.lstrip("@"), _items(1, ref.lstrip("@"), dated=True))
    sums = _batch(tmp_path, refs=("@a", "@b", "@c"))
    assert all(s["engine"] == "selenium" and s["error"] is None for s in sums)
    assert len(eng.built) == 1 and eng.built[0].quit_calls == 1
    assert [c["since"] for c in eng.selenium_calls] == [SINCE] * 3


def test_no_driver_is_built_when_ytdlp_answers_every_channel(eng, tmp_path):
    _batch(tmp_path, refs=("@a", "@b"))
    assert eng.built == []


def test_copy_in_window_mode_copies_the_listing_with_dates(eng, tmp_path, monkeypatch):
    items = _items(2, dated=True)
    eng.window = ("Veritasium", items, _meta())
    copied = {}
    monkeypatch.setattr(
        cli, "_copy_to_clipboard", lambda text: copied.setdefault("text", text) or True
    )
    assert cli.main(["--since", "2026-01-01", "--copy", "-o", str(tmp_path), "@veritasium"]) == 0
    assert "\n".join(format_related(items)) in copied["text"]
    assert "2026-03-01" in copied["text"]


def test_copy_joins_several_channels(eng, tmp_path, monkeypatch):
    eng.window = lambda ref: (ref.lstrip("@"), _items(1, ref.lstrip("@")), _meta())
    copied = {}
    monkeypatch.setattr(
        cli, "_copy_to_clipboard", lambda text: copied.setdefault("text", text) or True
    )
    _batch(tmp_path, refs=("@a", "@b"), copy=True)
    assert "Video a1" in copied["text"] and "Video b1" in copied["text"]
