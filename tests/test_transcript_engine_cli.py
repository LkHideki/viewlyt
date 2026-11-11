"""CLI-level behaviour of the transcript engines under failure.

* a yt-dlp failure must not recycle a healthy worker browser;
* `--transcript-engine auto` stops asking yt-dlp after consecutive failures (shared breaker);
* `--transcript-engine` on a listing-only run is a warned no-op, honoured in a pipeline;
* the "to scrape with" log line is true when no browser is built;
* the autouse network guard sits on the yt-dlp I/O boundaries.

No browser, no network: ``ytdl.video_transcript`` / ``cli.scrape_one`` /
``cli.build_primed_driver`` are scripted on ``viewlyt.cli``.
"""

from __future__ import annotations

import logging
import sys
import threading

import pytest
from conftest import FakeDriver

import viewlyt.cli as cli
from viewlyt import ytdl
from viewlyt.htmltext import slugify

SEGS = [("0:00", "alpha"), ("0:02", "beta"), ("0:04", "gamma")]
BREAKER_WARNING = "in a row"


def vid(n: int) -> str:
    return f"vid{n:08d}"


class Rig:
    """Scripts the three per-video boundaries and records every use.

    ``yt_fail``: ids whose yt-dlp call raises; ``yt_empty``: ids answering "no captions";
    ``scrape_fail_once``: ids whose FIRST page scrape raises (a browser-side failure).
    ``scrapes`` records ``(video_id, driver, driver.quit_calls at that moment)``."""

    def __init__(self, monkeypatch) -> None:
        self.yt_calls: list[str] = []
        self.yt_fail: set[str] = set()
        self.yt_empty: set[str] = set()
        self.scrape_fail_once: set[str] = set()
        self.drivers: list[FakeDriver] = []
        self.scrapes: list[tuple[str, FakeDriver, int]] = []
        self._lock = threading.Lock()
        monkeypatch.setattr(ytdl, "video_transcript", self._video_transcript)
        monkeypatch.setattr(cli, "build_primed_driver", self._build)
        monkeypatch.setattr(cli, "scrape_one", self._scrape)

    def _video_transcript(self, video_id):
        self.yt_calls.append(video_id)
        if video_id in self.yt_fail:
            raise RuntimeError(f"yt-dlp exploded on {video_id}")
        segs = [] if video_id in self.yt_empty else list(SEGS)
        return {"video_id": video_id, "title": f"YT {video_id}", "views_int": 5, "segments": segs}

    def _build(self, headless, user_data_dir):
        drv = FakeDriver()
        with self._lock:
            self.drivers.append(drv)
        return drv

    def _scrape(self, driver, url, **kw):
        v = url.rsplit("=", 1)[1]
        with self._lock:
            self.scrapes.append((v, driver, driver.quit_calls))
        if v in self.scrape_fail_once:
            self.scrape_fail_once.discard(v)
            raise RuntimeError(f"page scrape exploded on {v}")
        # the Selenium panel serves the transcript (with_transcript=True), else nothing
        transcript = list(SEGS) if kw.get("with_transcript") else []
        return v, f"Page {v}", [], transcript, [], 99


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def run(tmp_path, *argv: str, jobs: int = 1) -> int:
    return cli.main(["-o", str(tmp_path), "--no-tokens", "-j", str(jobs), *argv])


def ids(*ns: int) -> list[str]:
    return [f"https://www.youtube.com/watch?v={vid(n)}" for n in ns]


def breaker_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and BREAKER_WARNING in r.getMessage()
    ]


# --------------------------------------------------------------------------- #
# 1. A yt-dlp failure keeps the worker's healthy browser
# --------------------------------------------------------------------------- #
def test_a_ytdlp_failure_does_not_recycle_the_workers_browser(rig, tmp_path, capsys):
    rig.yt_fail = {vid(2)}
    rc = run(tmp_path, "-c", "-t", "--transcript-engine", "ytdlp", *ids(1, 2, 3))
    assert rc == 0  # v1, v3 fine; v2 failed after its retry
    # v1 built the driver; v2 failed in yt-dlp (twice) without touching it; v3 reused it.
    assert len(rig.drivers) == 1
    scraped = [(v, quits) for v, _drv, quits in rig.scrapes]
    assert scraped == [(vid(1), 0), (vid(3), 0)]  # never quit between videos
    assert all(drv is rig.drivers[0] for _v, drv, _q in rig.scrapes)
    assert rig.drivers[0].quit_calls == 1  # only the worker's final cleanup
    assert rig.yt_calls.count(vid(2)) == 2  # still the normal retry/fail accounting
    out, err = capsys.readouterr()
    assert "Done: 2/3 video(s) scraped, 1 failed" in out
    assert vid(2) in err and "yt-dlp exploded" in err


def test_the_retry_of_a_ytdlp_failure_still_happens_once(rig, tmp_path):
    rig.yt_fail = {vid(1)}
    summaries = cli.run_batch(
        [(vid(1), ids(1)[0])],
        jobs=1,
        headless=True,
        fallback=True,
        user_data_dir=None,
        out_dir=str(tmp_path),
        limit=5,
        max_viewports=1,
        expand_replies=False,
        max_replies=0,
        with_comments=False,
        with_transcript=True,
        with_related=False,
        related_limit=0,
        merge_comments=True,
        unify=False,
        unify_all=False,
        inner_progress=False,
        quiet=True,
        transcript_engine="ytdlp",
    )
    assert rig.yt_calls == [vid(1), vid(1)]
    assert len(summaries) == 1 and "yt-dlp exploded" in summaries[0]["error"]


def test_a_browser_side_failure_still_recycles_the_browser(rig, tmp_path, capsys):
    rig.scrape_fail_once = {vid(1)}
    rc = run(tmp_path, "-c", *ids(1, 2))
    assert rc == 0
    # v1's page scrape raised: that driver was quit and the retry/next video got a fresh one.
    assert len(rig.drivers) == 2
    assert rig.drivers[0].quit_calls == 1
    assert [v for v, _d, _q in rig.scrapes] == [vid(1), vid(2), vid(1)]
    assert rig.scrapes[1][1] is rig.drivers[1]
    assert "Done: 2/2 video(s) scraped" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 2. The `auto` breaker
# --------------------------------------------------------------------------- #
def test_three_consecutive_failures_stop_yt_dlp_for_the_rest_of_the_run(rig, tmp_path, caplog):
    rig.yt_fail = {vid(n) for n in range(1, 9)}
    assert run(tmp_path, "-t", *ids(*range(1, 9))) == 0
    assert rig.yt_calls == [vid(1), vid(2), vid(3)]  # 4th..8th never ask yt-dlp
    assert len(breaker_warnings(caplog)) == 1
    assert "Selenium" in breaker_warnings(caplog)[0]
    # every video still got its transcript, from the panel
    for n in range(1, 9):
        v = vid(n)
        assert (tmp_path / f"{slugify(f'Page {v}')}-{v}.transcript.md").exists()
    assert len(rig.scrapes) == 8


def test_summaries_say_selenium_once_the_breaker_is_open(rig, tmp_path):
    rig.yt_fail = {vid(n) for n in range(1, 6)}
    targets = [(vid(n), url) for n, url in zip(range(1, 6), ids(*range(1, 6)), strict=True)]
    summaries = _batch(tmp_path, targets)
    assert [s["transcript_engine"] for s in summaries] == ["selenium"] * 5
    assert all(s["error"] is None for s in summaries)


def test_a_success_resets_the_streak(rig, tmp_path, caplog):
    # f f ok f f ok: never 3 in a row, so yt-dlp is asked for every video.
    rig.yt_fail = {vid(1), vid(2), vid(4), vid(5)}
    assert run(tmp_path, "-t", *ids(1, 2, 3, 4, 5, 6)) == 0
    assert rig.yt_calls == [vid(n) for n in range(1, 7)]
    assert breaker_warnings(caplog) == []


def test_a_streak_restarting_after_a_success_can_still_trip_the_breaker(rig, tmp_path, caplog):
    # f f ok | f f f -> trips on the 6th call; the 7th and 8th never reach yt-dlp.
    rig.yt_fail = {vid(1), vid(2), vid(4), vid(5), vid(6), vid(7), vid(8)}
    assert run(tmp_path, "-t", *ids(*range(1, 9))) == 0
    assert rig.yt_calls == [vid(n) for n in range(1, 7)]
    assert len(breaker_warnings(caplog)) == 1


def test_an_empty_answer_is_not_a_failure_and_resets_the_streak(rig, tmp_path, caplog):
    # f f (no captions) f f: "no captions" is yt-dlp WORKING, not a failure.
    rig.yt_fail = {vid(1), vid(2), vid(4), vid(5)}
    rig.yt_empty = {vid(3)}
    assert run(tmp_path, "-t", *ids(1, 2, 3, 4, 5)) == 0
    assert rig.yt_calls == [vid(n) for n in range(1, 6)]
    assert breaker_warnings(caplog) == []


def test_only_failures_count_so_endless_empties_never_open_it(rig, tmp_path, caplog):
    rig.yt_empty = {vid(n) for n in range(1, 8)}
    assert run(tmp_path, "-t", *ids(*range(1, 8))) == 0
    assert len(rig.yt_calls) == 7 and breaker_warnings(caplog) == []


def test_the_ytdlp_engine_has_no_breaker(rig, tmp_path, caplog, capsys):
    rig.yt_fail = {vid(n) for n in range(1, 6)}
    rc = run(tmp_path, "-t", "--transcript-engine", "ytdlp", *ids(*range(1, 6)))
    assert rc == 1  # every video fails on its own (no fallback), as before
    assert len(rig.yt_calls) == 10  # each of the 5 asked twice: first try + the retry
    assert breaker_warnings(caplog) == []
    assert "Done: 0/5 video(s) scraped, 5 failed" in capsys.readouterr().out


def test_the_selenium_engine_never_asks_yt_dlp_and_has_no_breaker(rig, tmp_path, caplog):
    assert run(tmp_path, "-t", "--transcript-engine", "selenium", *ids(1, 2, 3, 4)) == 0
    assert rig.yt_calls == [] and breaker_warnings(caplog) == []


def test_the_breaker_is_per_run_not_global(rig, tmp_path, caplog):
    rig.yt_fail = {vid(1), vid(2), vid(3)}
    assert run(tmp_path, "-t", *ids(1, 2, 3)) == 0  # opens during run 1
    assert len(breaker_warnings(caplog)) == 1
    rig.yt_fail = set()
    rig.yt_calls.clear()
    assert run(tmp_path, "-t", *ids(4, 5)) == 0  # a new run asks yt-dlp again
    assert rig.yt_calls == [vid(4), vid(5)]


def test_the_breaker_holds_with_parallel_workers(rig, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)  # no worker stagger
    n_videos, jobs = 24, 3
    rig.yt_fail = {vid(n) for n in range(1, n_videos + 1)}
    assert run(tmp_path, "-t", *ids(*range(1, n_videos + 1)), jobs=jobs) == 0
    # Up to 3 failures open it; each of the other workers may already be mid-call.
    assert 3 <= len(rig.yt_calls) <= 3 + jobs - 1
    assert len(breaker_warnings(caplog)) == 1
    assert len(rig.scrapes) == n_videos  # nothing lost: the panel served them all


def test_failure_breaker_unit():
    b = cli._FailureBreaker(3)
    assert b.is_open is False
    assert [b.failure(), b.failure()] == [False, False]
    b.success()
    assert [b.failure(), b.failure(), b.failure()] == [False, False, True]  # True exactly once
    assert b.is_open is True
    assert b.failure() is False
    b.success()  # a late success from an in-flight call doesn't close it
    assert b.is_open is True


def test_failure_breaker_opens_exactly_once_under_threads():
    # Best-effort stress of the exactly-once contract (a tiny switch interval forces thread
    # switches). The critical section is a few bytecodes, so this is a smoke check for the
    # lock, not a proof: the deterministic guarantees are the single-thread unit test above.
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(200):
            b = cli._FailureBreaker(3)
            opened: list[bool] = []
            barrier = threading.Barrier(8)

            def hammer(b=b, opened=opened, barrier=barrier):
                barrier.wait()
                if b.failure():
                    opened.append(True)

            threads = [threading.Thread(target=hammer) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert opened == [True] and b.is_open
    finally:
        sys.setswitchinterval(old_interval)


def _batch(tmp_path, targets, **over):
    kw = dict(
        jobs=1,
        headless=True,
        fallback=True,
        user_data_dir=None,
        out_dir=str(tmp_path),
        limit=5,
        max_viewports=1,
        expand_replies=False,
        max_replies=0,
        with_comments=False,
        with_transcript=True,
        with_related=False,
        related_limit=0,
        merge_comments=True,
        unify=False,
        unify_all=False,
        inner_progress=False,
        quiet=True,
    )
    kw.update(over)
    return cli.run_batch(list(targets), **kw)


# --------------------------------------------------------------------------- #
# 3. --transcript-engine on a listing-only run
# --------------------------------------------------------------------------- #
def _listing(monkeypatch, n=2):
    items = [
        {
            "video_id": vid(i),
            "title": f"Listed {i}",
            "views": "",
            "url": f"https://www.youtube.com/watch?v={vid(i)}",
        }
        for i in range(1, n + 1)
    ]
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order="recent": ("Chan", items))
    monkeypatch.setattr(
        cli.ytdl, "channel_entries_window", lambda ref, s, u, **k: ("Chan", items, _meta())
    )


def _meta():
    return {
        "probes": 1,
        "flat_requests": 1,
        "scanned": 5,
        "truncated_by_cap": False,
        "non_monotonic": False,
    }


def _engine_warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "--transcript-engine" in r.getMessage()
    ]


@pytest.mark.parametrize("engine", ["selenium", "ytdlp"])
@pytest.mark.parametrize("listing", [["-C", "30"], ["--since", "90d"]])
def test_transcript_engine_on_a_listing_only_run_warns_and_does_nothing(
    monkeypatch, tmp_path, caplog, listing, engine
):
    _listing(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("a listing-only run must not scrape")

    monkeypatch.setattr(cli, "run_batch", boom)
    rc = cli.main(["-o", str(tmp_path), *listing, "--transcript-engine", engine, "@veritasium"])
    assert rc == 0
    (msg,) = _engine_warnings(caplog)
    assert "no effect" in msg and "listing-only" in msg
    assert (tmp_path / "chan.channel.md").exists()


def test_the_default_engine_on_a_listing_only_run_does_not_warn(monkeypatch, tmp_path, caplog):
    _listing(monkeypatch)
    assert cli.main(["-o", str(tmp_path), "-C", "30", "@veritasium"]) == 0
    assert _engine_warnings(caplog) == []
    # an explicit `auto` is the default: nothing to warn about either
    assert cli.main(["-o", str(tmp_path), "-C", "30", "--transcript-engine", "auto", "@x_x"]) == 0
    assert _engine_warnings(caplog) == []


_LISTINGS = [["-C", "30"], ["--since", "90d"], ["-C", "5", "--since", "1y"]]


@pytest.mark.parametrize("listing", _LISTINGS)
@pytest.mark.parametrize("selector", [["-t"], ["-u"], ["--unify-all"]])
def test_in_a_pipeline_that_collects_a_transcript_the_engine_is_honoured_and_not_warned_about(
    monkeypatch, tmp_path, caplog, listing, selector
):
    _listing(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(kw) or [])
    argv = ["-o", str(tmp_path), *listing, *selector, "--transcript-engine", "selenium"]
    assert cli.main([*argv, "@veritasium"]) == 0
    assert [c["transcript_engine"] for c in calls] == ["selenium"]
    assert _engine_warnings(caplog) == []


@pytest.mark.parametrize("listing", _LISTINGS)
@pytest.mark.parametrize("selector", [["-c"], ["-r", "3"], ["-c", "-r", "3"]])
def test_in_a_pipeline_without_a_transcript_the_engine_is_a_warned_no_op(
    monkeypatch, tmp_path, caplog, listing, selector
):
    """Was silent: the value still reaches ``run_batch`` (harmless), but nothing will read it,
    and the user typed it expecting an effect. ONE warning, from the per-video flow (the
    listing-only warning in ``main`` is for runs with no selector at all)."""
    _listing(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(kw) or [])
    argv = ["-o", str(tmp_path), *listing, *selector, "--transcript-engine", "ytdlp"]
    assert cli.main([*argv, "@veritasium"]) == 0
    assert [c["transcript_engine"] for c in calls] == ["ytdlp"]
    (record,) = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "--transcript-engine" in r.getMessage()
    ]
    assert "no transcript is being collected" in record.getMessage()


def test_the_pipeline_default_engine_is_auto(monkeypatch, tmp_path):
    _listing(monkeypatch)
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(kw) or [])
    assert cli.main(["-o", str(tmp_path), "-C", "30", "-t", "@veritasium"]) == 0
    assert calls[0]["transcript_engine"] == "auto"


def test_a_plain_video_run_never_warns_about_the_engine(monkeypatch, tmp_path, caplog):
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(kw) or [])
    argv = ["-o", str(tmp_path), "-t", "--transcript-engine", "selenium", vid(1)]
    assert cli.main(argv) == 0
    assert calls[0]["transcript_engine"] == "selenium" and _engine_warnings(caplog) == []


@pytest.mark.parametrize("engine", ["ytdlp", "selenium"])
@pytest.mark.parametrize("selector", [["-c"], ["-r", "3"], ["-c", "-r", "2"]])
def test_a_plain_run_without_a_transcript_warns_that_the_engine_does_nothing(
    monkeypatch, tmp_path, caplog, selector, engine
):
    calls: list[dict] = []
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: calls.append(kw) or [])
    argv = ["-o", str(tmp_path), *selector, "--transcript-engine", engine, vid(1)]
    assert cli.main(argv) == 0
    assert [c["transcript_engine"] for c in calls] == [engine]
    (record,) = [r for r in caplog.records if "--transcript-engine" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "no effect" in record.getMessage() and "no transcript" in record.getMessage()


@pytest.mark.parametrize(
    "argv",
    [
        ["--transcript-engine", "ytdlp"],  # no selector: the transcript is the default
        ["-t", "--transcript-engine", "selenium"],
        ["--transcript-only", "--transcript-engine", "ytdlp"],
        ["-c", "-t", "--transcript-engine", "selenium"],
        ["-u", "--transcript-engine", "selenium"],  # collect-everything includes it
        ["--unify-all", "--transcript-engine", "ytdlp"],
        ["-c", "--transcript-engine", "auto"],  # the default engine: nothing to explain
        ["-c"],
    ],
)
def test_no_engine_warning_when_a_transcript_is_collected_or_the_engine_is_auto(
    monkeypatch, tmp_path, caplog, argv
):
    monkeypatch.setattr(cli, "run_batch", lambda targets, **kw: [])
    assert cli.main(["-o", str(tmp_path), *argv, vid(1)]) == 0
    assert _engine_warnings(caplog) == []


def test_a_listing_only_run_gets_exactly_one_engine_warning(monkeypatch, tmp_path, caplog):
    _listing(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("a listing-only run must not scrape")

    monkeypatch.setattr(cli, "run_batch", boom)
    argv = ["-o", str(tmp_path), "-C", "3", "--transcript-engine", "selenium", "@veritasium"]
    assert cli.main(argv) == 0
    (msg,) = _engine_warnings(caplog)
    assert "listing-only" in msg  # main's warning, not the per-video one (never reached)


# --------------------------------------------------------------------------- #
# 4. The "to scrape with" log line is true without a browser
# --------------------------------------------------------------------------- #
def test_the_scrape_log_line_talks_about_workers_not_browsers(rig, tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)  # no worker stagger
    caplog.set_level(logging.INFO)
    rig.yt_empty = set()
    assert run(tmp_path, "-t", *ids(1, 2), jobs=2) == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert "2 video(s) to scrape with up to 2 worker(s)" in msgs
    assert not any("browser instance" in m for m in msgs)
    assert rig.drivers == []  # ... and indeed no browser was built (yt-dlp served both)


# --------------------------------------------------------------------------- #
# 5. The autouse guard sits on the yt-dlp I/O boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "call",
    [
        lambda: ytdl._extract("https://www.youtube.com/watch?v=x", {}),
        lambda: ytdl._extract_with_captions("dQw4w9WgXcQ"),
        lambda: ytdl.video_transcript("dQw4w9WgXcQ"),
        lambda: ytdl.video_info("dQw4w9WgXcQ"),
        lambda: ytdl.channel_info("@veritasium"),
        lambda: ytdl.channel_entries("@veritasium", 3),
        lambda: ytdl.video_upload_date("dQw4w9WgXcQ"),
    ],
)
def test_the_suite_cannot_reach_the_network_through_any_ytdl_entry_point(call):
    with pytest.raises(RuntimeError, match="network disabled in tests"):
        call()


def test_the_guard_also_covers_a_caller_that_bound_the_function_early():
    # The old guard patched the string target "viewlyt.cli.ytdl.video_transcript"; a caller
    # holding its own reference to the function bypassed it. At the boundary it cannot.
    held = ytdl.video_transcript
    with pytest.raises(RuntimeError, match="network disabled in tests"):
        held("dQw4w9WgXcQ")
