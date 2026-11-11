"""Channel -> per-video pipeline: ``-C N`` / ``--since`` plus a product selector.

Alone, ``-C``/``--since`` only LIST a channel. With ``-c``/``-t``/``--transcript-only``/``-r``/
``-u``/``--unify-all`` the listed videos go through the same per-video flow ``vl <urls>`` uses.

No browser, no network: the listing engines (``ytdl.channel_entries`` /
``channel_entries_window``, ``scraper.collect_channel_videos``) and the per-video boundaries
(``ytdl.video_transcript``, ``scrape_one``, the driver builder, the clipboard) are patched on
``viewlyt.cli``, like the existing integration tests do.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest
from conftest import FakeDriver
from conftest import make_comment as _c

import viewlyt.cli as cli
from viewlyt.htmltext import extract_video_id, format_related, slugify

TODAY = date(2026, 7, 23)
SEGS = [("0:00", "alpha"), ("0:02", "beta"), ("0:04", "gamma")]


def vid(n: int) -> str:
    """A syntactically valid 11-char video id."""
    return f"vid{n:08d}"


def item(n: int, **over) -> dict:
    v = vid(n)
    return {
        "video_id": v,
        "title": f"Listed {n}",
        "views": f"{n * 1000:,} views",
        "views_int": n * 1000,
        "url": f"https://www.youtube.com/watch?v={v}",
    } | over


def meta(scanned: int = 64) -> dict:
    return {
        "probes": 4,
        "flat_requests": 1,
        "scanned": scanned,
        "truncated_by_cap": False,
        "non_monotonic": False,
    }


class Rig:
    """Scripts the listing engines and the per-video boundaries; records every call.

    Listing: :meth:`plain` / :meth:`window` / :meth:`selenium` script one channel ref each
    (a result or an exception). Per-video: ``ytdl.video_transcript`` answers ``SEGS`` titled
    ``"Title <id>"`` unless the id is in ``yt_fail``; ``scrape_one`` answers a page scrape
    titled ``"Scraped <id>"`` with one comment, unless the id is in ``scrape_fail``."""

    def __init__(self, monkeypatch) -> None:
        self._plain: dict[str, object] = {}
        self._window: dict[str, object] = {}
        self._selenium: dict[str, object] = {}
        self.plain_calls: list[tuple] = []
        self.window_calls: list[dict] = []
        self.selenium_calls: list[dict] = []
        self.yt_calls: list[str] = []
        self.yt_fail: set[str] = set()
        self.scrape_fail: set[str] = set()
        self.drivers: list[FakeDriver] = []
        self.scrapes: list[dict] = []  # {"video_id", "url", **kwargs}
        self.copied: list[str] = []
        monkeypatch.setattr(cli.ytdl, "channel_entries", self._do_plain)
        monkeypatch.setattr(cli.ytdl, "channel_entries_window", self._do_window)
        monkeypatch.setattr(cli.ytdl, "fill_dates", lambda items, **k: 0)
        monkeypatch.setattr(cli, "collect_channel_videos", self._do_selenium)
        monkeypatch.setattr(cli.ytdl, "video_transcript", self._video_transcript)
        monkeypatch.setattr(cli, "build_primed_driver", self._build)
        monkeypatch.setattr(cli, "scrape_one", self._scrape)
        monkeypatch.setattr(cli, "_today_utc", lambda: TODAY)
        monkeypatch.setattr(
            cli, "_copy_to_clipboard", lambda text: self.copied.append(text) or True
        )

    # -- listing scripts
    def plain(self, ref, name, *ns) -> Rig:
        self._plain[ref] = (name, [item(n) for n in ns])
        return self

    def window(self, ref, name, *ns, scanned=None) -> Rig:
        items = [item(n) for n in ns]
        self._window[ref] = (name, items, meta(scanned if scanned is not None else 64))
        return self

    def selenium(self, ref, result) -> Rig:
        self._selenium[ref] = result
        return self

    # -- listing fakes
    @staticmethod
    def _answer(table, ref):
        res = table.get(ref, RuntimeError(f"no listing scripted for {ref}"))
        if isinstance(res, Exception):
            raise res
        return res

    def _do_plain(self, ref, n, order="recent"):
        self.plain_calls.append((ref, n, order))
        return self._answer(self._plain, ref)

    def _do_window(self, ref, since, until, *, order="recent", cap=5000):
        self.window_calls.append({"ref": ref, "since": since, "until": until, "order": order})
        return self._answer(self._window, ref)

    def _do_selenium(
        self, driver, ref, limit, order="recent", progress=True, *, since=None, until=None
    ):
        self.selenium_calls.append({"ref": ref, "limit": limit, "order": order})
        res = self._selenium.get(ref, ("", []))
        if isinstance(res, Exception):
            raise res
        return res

    # -- per-video fakes
    def _video_transcript(self, video_id):
        self.yt_calls.append(video_id)
        if video_id in self.yt_fail:
            raise RuntimeError(f"yt-dlp exploded on {video_id}")
        return {
            "video_id": video_id,
            "title": f"Title {video_id}",
            "views_int": 1234,
            "segments": list(SEGS),
        }

    def _build(self, headless, user_data_dir):
        drv = FakeDriver()
        self.drivers.append(drv)
        return drv

    def _scrape(self, driver, url, **kw):
        v = extract_video_id(url)
        self.scrapes.append({"video_id": v, "url": url, **kw})
        if v in self.scrape_fail:
            raise RuntimeError(f"page scrape exploded on {v}")
        related = [{"title": f"Rel of {v}", "views": "9 views", "url": "https://y/watch?v=r"}]
        return v, f"Scraped {v}", [_c("@u", f"hi from {v}")], list(SEGS), related, 777


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


def run(tmp_path, *argv: str, jobs: int | None = 1) -> int:
    pre = ["-o", str(tmp_path), "--no-tokens"]
    if jobs is not None:
        pre += ["-j", str(jobs)]
    return cli.main([*pre, *argv])


def tfile(tmp_path, n: int):
    v = vid(n)
    return tmp_path / f"{slugify(f'Title {v}')}-{v}.transcript.md"


def scraped_titles(tmp_path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir())


@pytest.fixture
def no_batch(monkeypatch):
    """Make any ``run_batch`` call a test failure."""

    def boom(*a, **k):
        raise AssertionError("run_batch must not be called")

    monkeypatch.setattr(cli, "run_batch", boom)


@pytest.fixture
def batch_calls(monkeypatch):
    """Replace ``run_batch`` with a recorder that scrapes nothing (returns no summaries)."""
    calls: list[dict] = []

    def fake(targets, **kw):
        calls.append({"targets": list(targets), **kw})
        return []

    monkeypatch.setattr(cli, "run_batch", fake)
    return calls


# --------------------------------------------------------------------------- #
# The happy path: list, then transcribe each
# --------------------------------------------------------------------------- #
def test_c_with_t_writes_the_listing_and_one_transcript_per_video_without_a_browser(
    rig, tmp_path, capsys
):
    rig.plain("@veritasium", "Veritasium", 1, 2)
    assert run(tmp_path, "-C", "2", "-t", "@veritasium") == 0

    listing = tmp_path / "veritasium.channel.md"
    assert (
        listing.read_text(encoding="utf-8") == "\n".join(format_related([item(1), item(2)])) + "\n"
    )
    assert tfile(tmp_path, 1).read_text(encoding="utf-8") == "alpha beta\ngamma\n"
    assert tfile(tmp_path, 2).exists()
    assert len(list(tmp_path.iterdir())) == 3
    # yt-dlp served the listing AND the transcripts: no browser, in listing order.
    assert rig.drivers == [] and rig.scrapes == [] and rig.selenium_calls == []
    assert rig.yt_calls == [vid(1), vid(2)]
    assert rig.plain_calls == [("@veritasium", 2, "recent")]

    out = capsys.readouterr().out
    assert "Done: 1/1 channel(s) listed" in out
    assert "Done: 2/2 video(s) scraped" in out
    assert out.index("channel(s) listed") < out.index("video(s) scraped")


def test_transcript_only_flag_is_a_selector_too(rig, tmp_path):
    rig.plain("@veritasium", "Veritasium", 1)
    assert run(tmp_path, "-C", "1", "--transcript-only", "@veritasium") == 0
    assert tfile(tmp_path, 1).exists() and rig.yt_calls == [vid(1)]


def test_since_with_t_goes_through_the_window_engine(rig, tmp_path):
    rig.window("@veritasium", "Veritasium", 1, 2, 3)
    assert run(tmp_path, "--since", "90d", "-t", "@veritasium") == 0
    assert rig.plain_calls == []
    assert rig.window_calls[0]["since"] == date(2026, 4, 24)
    assert rig.window_calls[0]["until"] == TODAY
    assert (tmp_path / "veritasium.channel.md").exists()
    assert [tfile(tmp_path, n).exists() for n in (1, 2, 3)] == [True] * 3
    assert rig.drivers == []


def test_since_with_c_ceiling_and_t_scrapes_only_the_capped_videos(rig, tmp_path):
    rig.window("@veritasium", "Veritasium", 1, 2, 3, 4)
    assert run(tmp_path, "-C", "2", "--since", "1y", "-t", "@veritasium") == 0
    assert rig.yt_calls == [vid(1), vid(2)]
    assert not tfile(tmp_path, 3).exists()


def test_c_with_comments_and_transcript_goes_through_scrape_one(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "-c", "-t", "@chan") == 0
    assert [s["video_id"] for s in rig.scrapes] == [vid(1), vid(2)]
    for s in rig.scrapes:
        # yt-dlp serves the transcript text; the page scrape is for the comments only.
        assert s["with_comments"] is True and s["with_transcript"] is False
        assert s["with_related"] is False
    assert len(rig.drivers) == 1  # one worker, one reused browser
    for n in (1, 2):
        v = vid(n)
        assert (tmp_path / f"{slugify(f'Scraped {v}')}-{v}.md").exists()  # comments
        assert (tmp_path / f"{slugify(f'Scraped {v}')}-{v}.transcript.md").exists()


def test_c_with_comments_alone_does_not_touch_ytdlp_transcripts(rig, tmp_path):
    rig.plain("@chan", "Chan", 1)
    assert run(tmp_path, "-C", "1", "-c", "@chan") == 0
    assert rig.yt_calls == []
    assert rig.scrapes[0]["with_comments"] is True and rig.scrapes[0]["with_transcript"] is False


def test_c_with_related_scrapes_related_only(rig, tmp_path):
    rig.plain("@chan", "Chan", 1)
    assert run(tmp_path, "-C", "1", "-r", "3", "@chan") == 0
    (s,) = rig.scrapes
    assert s["with_related"] is True and s["related_limit"] == 3
    assert s["with_comments"] is False and s["with_transcript"] is False
    v = vid(1)
    assert (tmp_path / f"{slugify(f'Scraped {v}')}-{v}.related.md").exists()


def test_c_with_unify_collects_everything_and_writes_one_unified_file_per_video(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "-u", "@chan") == 0
    for s in rig.scrapes:  # the collect-everything rule of the per-video flow
        assert s["with_comments"] is True and s["with_related"] is True
        assert s["related_limit"] == cli._UNIFY_DEFAULT_RELATED
    for n in (1, 2):
        v = vid(n)
        doc = (tmp_path / f"{slugify(f'Scraped {v}')}-{v}.unified.md").read_text(encoding="utf-8")
        assert "## Comments" in doc and "## Transcript" in doc and "## Related videos" in doc
    assert not list(tmp_path.glob("*.transcript.md"))


def test_unify_with_r_keeps_the_related_count_override(rig, tmp_path):
    rig.plain("@chan", "Chan", 1)
    assert run(tmp_path, "-C", "1", "-u", "-r", "3", "@chan") == 0
    assert rig.scrapes[0]["related_limit"] == 3 and rig.scrapes[0]["with_comments"] is True


def test_popular_order_listing_via_selenium_then_unify(rig, tmp_path):
    # yt-dlp can't list "popular": the Selenium fallback lists, the pipeline carries on.
    rig._plain["@chan"] = NotImplementedError("popular")
    rig.selenium("@chan", ("Chan", [item(5), item(6)]))
    assert run(tmp_path, "-C", "20", "--order", "popular", "-u", "@chan") == 0
    assert rig.selenium_calls == [{"ref": "@chan", "limit": 20, "order": "popular"}]
    assert [s["video_id"] for s in rig.scrapes] == [vid(5), vid(6)]


def test_unify_all_writes_one_document_in_listing_order(rig, tmp_path):
    rig.plain("@alpha", "Alpha", 3, 1)
    rig.plain("@beta", "Beta", 2)
    assert run(tmp_path, "-C", "5", "--unify-all", "@alpha", "@beta") == 0
    doc = (tmp_path / cli.UNIFIED_ALL_FILENAME).read_text(encoding="utf-8")
    heads = [ln for ln in doc.splitlines() if ln.startswith("# ")]
    assert heads == [f"# Scraped {vid(n)}" for n in (3, 1, 2)]  # not sorted: listing order
    assert not list(tmp_path.glob("*.unified.md"))
    assert (tmp_path / "alpha.channel.md").exists() and (tmp_path / "beta.channel.md").exists()


def test_since_with_unify_all(rig, tmp_path):
    rig.window("@chan", "Chan", 1, 2)
    assert run(tmp_path, "--since", "1y", "--unify-all", "@chan") == 0
    doc = (tmp_path / cli.UNIFIED_ALL_FILENAME).read_text(encoding="utf-8")
    assert doc.count("\n# ") + doc.startswith("# ") == 2


def test_the_listing_phase_is_unchanged_in_a_pipeline(rig, tmp_path, capsys):
    rig.plain("@chan", "Chan", 1, 2)
    run(tmp_path, "-C", "2", "-t", "@chan")
    out = capsys.readouterr().out
    assert f"  ✓ Chan  2 videos (recent, yt-dlp) -> {tmp_path / 'chan.channel.md'}" in out


# --------------------------------------------------------------------------- #
# Targets: order, de-duplication, skipped items
# --------------------------------------------------------------------------- #
def test_targets_follow_listing_order_channel_by_channel(rig, tmp_path, batch_calls):
    rig.plain("@alpha", "Alpha", 3, 1)
    rig.plain("@beta", "Beta", 2)
    assert run(tmp_path, "-C", "5", "-t", "@alpha", "@beta") == 0
    (call,) = batch_calls
    assert call["targets"] == [
        (vid(n), f"https://www.youtube.com/watch?v={vid(n)}") for n in (3, 1, 2)
    ]


def test_a_video_shared_by_two_channels_is_scraped_once(rig, tmp_path, batch_calls):
    rig.plain("@alpha", "Alpha", 1, 2)
    rig.plain("@beta", "Beta", 2, 3)
    assert run(tmp_path, "-C", "5", "-t", "@alpha", "@beta") == 0
    assert [v for v, _ in batch_calls[0]["targets"]] == [vid(1), vid(2), vid(3)]
    # ... but each channel's own listing file still shows its full list.
    assert (tmp_path / "alpha.channel.md").read_text(encoding="utf-8").count("\n") == 2
    assert (tmp_path / "beta.channel.md").read_text(encoding="utf-8").count("\n") == 2


def test_a_shared_video_is_transcribed_once_end_to_end(rig, tmp_path, capsys):
    rig.plain("@alpha", "Alpha", 1, 2)
    rig.plain("@beta", "Beta", 2, 3)
    assert run(tmp_path, "-C", "5", "-t", "@alpha", "@beta") == 0
    assert sorted(rig.yt_calls) == [vid(1), vid(2), vid(3)]
    assert "Done: 3/3 video(s) scraped" in capsys.readouterr().out


def test_items_without_a_video_id_are_skipped_and_a_missing_url_is_rebuilt(
    rig, tmp_path, batch_calls
):
    rig._plain["@chan"] = (
        "Chan",
        [item(1, video_id=""), item(2, url=""), item(3, video_id=None), item(4)],
    )
    assert run(tmp_path, "-C", "5", "-t", "@chan") == 0
    assert batch_calls[0]["targets"] == [
        (vid(2), f"https://www.youtube.com/watch?v={vid(2)}"),
        (vid(4), f"https://www.youtube.com/watch?v={vid(4)}"),
    ]


def test_summaries_expose_the_listed_items(rig, tmp_path):
    rig.plain("@ok", "Okay", 1, 2)
    sums = cli.run_channel_batch(
        ["@ok", "@broken"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=False,
        user_data_dir=None,
        quiet=True,
    )
    ok, broken = sums
    assert ok["items"] == [item(1), item(2)]  # n is applied by the engine, not re-cut here
    assert broken["error"] and broken["items"] == []


# --------------------------------------------------------------------------- #
# Listing failures and empty windows
# --------------------------------------------------------------------------- #
def test_a_failed_summary_is_never_scraped_even_if_it_carries_items(tmp_path, monkeypatch):
    summaries = [
        {"ref": "@bad", "channel": "", "error": "boom", "items": [item(1)]},
        {"ref": "@ok", "channel": "Ok", "error": None, "items": [item(2)]},
    ]
    assert cli._targets_from_listing(summaries) == (
        [(vid(2), f"https://www.youtube.com/watch?v={vid(2)}")],
        1,
    )


def test_one_channel_failing_and_one_ok_proceeds_with_the_videos_it_got(rig, tmp_path, capsys):
    rig.plain("@good", "Good", 1, 2)  # @bad has no script: yt-dlp raises, Selenium finds nothing
    assert run(tmp_path, "-C", "5", "-t", "@bad", "@good") == 0
    assert rig.yt_calls == [vid(1), vid(2)]
    out, err = capsys.readouterr()
    assert "Done: 1/2 channel(s) listed, 1 failed" in out
    assert "Done: 2/2 video(s) scraped" in out
    assert "@bad" in err and "yt-dlp and Selenium both failed" in err


def test_every_channel_failing_exits_one_and_never_starts_the_batch(
    rig, tmp_path, no_batch, caplog, capsys
):
    assert run(tmp_path, "-C", "5", "-t", "@bad", "@worse") == 1
    assert rig.yt_calls == [] and rig.scrapes == []
    assert list(tmp_path.iterdir()) == []
    out, err = capsys.readouterr()
    assert "Done: 0/2 channel(s) listed, 2 failed" in out
    assert "@bad" in err and "@worse" in err
    assert any("nothing to scrape" in r.getMessage() for r in caplog.records)


def test_a_genuinely_empty_window_exits_zero_and_never_starts_the_batch(
    rig, tmp_path, no_batch, caplog, capsys
):
    caplog.set_level(logging.INFO)
    rig.window("@chan", "Chan", scanned=40)
    assert run(tmp_path, "--since", "2026-07-01", "-t", "@chan") == 0
    assert rig.selenium_calls == [] and rig.yt_calls == [] and rig.drivers == []
    assert list(tmp_path.iterdir()) == []
    out, err = capsys.readouterr()
    assert "(no videos in the window)" in out
    assert "video(s) scraped" not in out and "✗" not in out + err
    assert any(
        "nothing to scrape" in r.getMessage() and r.levelno == logging.INFO for r in caplog.records
    )


def test_every_window_empty_across_several_channels_exits_zero(rig, tmp_path, no_batch):
    rig.window("@aaa", "Aaa", scanned=10)
    rig.window("@bbb", "Bbb", scanned=10)
    assert run(tmp_path, "--since", "30d", "-t", "@aaa", "@bbb") == 0


def test_a_failed_channel_plus_an_empty_window_has_nothing_to_scrape_and_the_listing_exit_code(
    rig, tmp_path, no_batch, capsys
):
    # Same rule as listing-only: 1 only when EVERY channel failed. The failure is
    # still reported on stderr.
    rig.window("@empty", "Empty", scanned=10)
    rig._window["@bad"] = RuntimeError("boom")
    assert run(tmp_path, "--since", "30d", "-t", "@empty", "@bad") == 0
    assert "@bad" in capsys.readouterr().err


def test_an_empty_window_beside_a_populated_one_scrapes_the_populated_one(rig, tmp_path):
    rig.window("@empty", "Empty", scanned=10)
    rig.window("@full", "Full", 7)
    assert run(tmp_path, "--since", "30d", "-t", "@empty", "@full") == 0
    assert rig.yt_calls == [vid(7)]


def test_the_handoff_is_announced(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig.plain("@alpha", "Alpha", 1, 2)
    rig.plain("@beta", "Beta", 3)
    run(tmp_path, "-C", "5", "-t", "@alpha", "@beta")
    msgs = [r.getMessage() for r in caplog.records]
    assert "3 video(s) from 2 channel(s) listed -> scraping them now" in msgs
    # between the listing's own log line and the per-video flow's
    i = msgs.index("3 video(s) from 2 channel(s) listed -> scraping them now")
    assert any("channel(s) to list" in m for m in msgs[:i])
    assert any("video(s) to scrape with" in m for m in msgs[i:])


# --------------------------------------------------------------------------- #
# --copy, -j, token report, exit code of the per-video flow
# --------------------------------------------------------------------------- #
def test_copy_copies_the_per_video_output_once(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "-t", "--copy", "@chan") == 0
    assert len(rig.copied) == 1
    assert "alpha beta" in rig.copied[0]
    assert "Listed" not in rig.copied[0] and "views." not in rig.copied[0]  # not the listing


def test_copy_with_unify_all_copies_the_unified_document_once(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "--unify-all", "--copy", "@chan") == 0
    assert len(rig.copied) == 1
    assert f"# Scraped {vid(1)}" in rig.copied[0] and f"# Scraped {vid(2)}" in rig.copied[0]


def test_listing_only_copy_still_copies_the_listing(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "--copy", "@chan") == 0
    assert len(rig.copied) == 1
    assert "\n".join(format_related([item(1), item(2)])) in rig.copied[0]


@pytest.mark.parametrize(
    ("argv", "expected_jobs"),
    [([], 3), (["-j", "2"], 2), (["-j", "9"], 3), (["-j", "1"], 1)],
)
def test_jobs_is_respected_and_capped_by_the_video_count(
    rig, tmp_path, batch_calls, argv, expected_jobs
):
    rig.plain("@chan", "Chan", 1, 2, 3)
    assert run(tmp_path, "-C", "3", "-t", *argv, "@chan", jobs=None) == 0
    assert batch_calls[0]["jobs"] == expected_jobs


def test_per_video_options_reach_the_batch(rig, tmp_path, batch_calls):
    rig.plain("@chan", "Chan", 1)
    argv = ["-C", "1", "-c", "--limit-comments", "7", "--limit-replies", "2", "--no-replies"]
    assert run(tmp_path, *argv, "--headed", "--ts", "--no-merge-comments", "@chan") == 0
    (call,) = batch_calls
    assert call["limit"] == 7 and call["max_replies"] == 2 and call["expand_replies"] is False
    assert call["headless"] is False and call["with_ts"] is True
    assert call["merge_comments"] is False and call["with_comments"] is True
    assert call["out_dir"] == str(tmp_path)


def test_one_failing_video_does_not_sink_the_rest(rig, tmp_path, capsys):
    rig.plain("@chan", "Chan", 1, 2, 3)
    rig.yt_fail = {vid(2)}
    rc = run(tmp_path, "-C", "3", "-t", "--transcript-engine", "ytdlp", "@chan")
    assert rc == 0
    assert tfile(tmp_path, 1).exists() and tfile(tmp_path, 3).exists()
    assert not tfile(tmp_path, 2).exists()
    assert (tmp_path / "chan.channel.md").exists()  # the listing is untouched by it
    out, err = capsys.readouterr()
    assert "Done: 2/3 video(s) scraped, 1 failed" in out
    assert vid(2) in err
    assert rig.yt_calls.count(vid(2)) == 2  # one automatic retry, as in plain batches


def test_every_video_failing_is_the_per_video_flows_exit_code(rig, tmp_path):
    rig.plain("@chan", "Chan", 1, 2)
    rig.yt_fail = {vid(1), vid(2)}
    rc = run(tmp_path, "-C", "2", "-t", "--transcript-engine", "ytdlp", "@chan")
    assert rc == 1
    assert (tmp_path / "chan.channel.md").exists()


def test_page_scrape_failure_isolation_with_comments(rig, tmp_path, capsys):
    rig.plain("@chan", "Chan", 1, 2)
    rig.scrape_fail = {vid(1)}
    assert run(tmp_path, "-C", "2", "-c", "@chan") == 0
    assert "Done: 1/2 video(s) scraped, 1 failed" in capsys.readouterr().out


def test_the_token_report_is_the_per_video_one_only(rig, tmp_path, monkeypatch):
    reports: list[list[dict]] = []
    monkeypatch.setattr(cli, "_print_token_report", lambda ok, budget_tokens: reports.append(ok))
    rig.plain("@chan", "Chan", 1, 2)
    assert cli.main(["-o", str(tmp_path), "-j", "1", "-C", "2", "-t", "@chan"]) == 0
    assert len(reports) == 1
    assert [s["video_id"] for s in reports[0]] == [vid(1), vid(2)]
    assert all("channel_file" not in s for s in reports[0])


def test_listing_only_keeps_its_own_token_report(rig, tmp_path, monkeypatch):
    reports: list[list[dict]] = []
    monkeypatch.setattr(cli, "_print_token_report", lambda ok, budget_tokens: reports.append(ok))
    rig.plain("@chan", "Chan", 1, 2)
    assert cli.main(["-o", str(tmp_path), "-C", "2", "@chan"]) == 0
    assert len(reports) == 1 and "channel_file" in reports[0][0]


# --------------------------------------------------------------------------- #
# Listing-only stays listing-only
# --------------------------------------------------------------------------- #
def test_c_alone_never_scrapes(rig, tmp_path, no_batch, capsys):
    rig.plain("@chan", "Chan", 1, 2)
    assert run(tmp_path, "-C", "2", "@chan") == 0
    assert rig.yt_calls == [] and rig.scrapes == [] and rig.drivers == []
    assert [p.name for p in tmp_path.iterdir()] == ["chan.channel.md"]
    out = capsys.readouterr().out
    assert "channel(s) listed" in out and "video(s) scraped" not in out


def test_since_alone_never_scrapes(rig, tmp_path, no_batch):
    rig.window("@chan", "Chan", 1, 2)
    assert run(tmp_path, "--since", "90d", "@chan") == 0
    assert rig.yt_calls == [] and rig.scrapes == []


def test_no_selector_means_the_handoff_line_is_never_logged(rig, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig.plain("@chan", "Chan", 1)
    run(tmp_path, "-C", "1", "@chan")
    assert not any("scraping them now" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Usage errors that remain
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (["--until", "30d", "-t"], "--until needs --since"),
        (["--since", "banana", "-t"], "--since"),
        (["--since", "30d", "--until", "soon", "-c"], "--until"),
        (["--since", "2026-07-01", "--until", "2026-06-01", "-t"], "empty"),
        (["--since", "90d", "--order", "popular", "-t"], "--order popular"),
        (["-C", "5", "--scan-cap", "-1", "-t"], "--scan-cap"),
    ],
)
def test_usage_errors_still_exit_two_before_any_work(
    rig, tmp_path, no_batch, caplog, argv, fragment
):
    assert run(tmp_path, *argv, "@chan") == 2
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and fragment in errors[0]
    assert rig.plain_calls == [] and rig.window_calls == [] and rig.yt_calls == []
    assert list(tmp_path.iterdir()) == []


def test_popular_with_a_window_and_a_selector_is_still_a_usage_error(rig, tmp_path, no_batch):
    assert run(tmp_path, "--since", "1y", "--order", "popular", "--unify-all", "@chan") == 2


def test_unify_and_unify_all_stay_mutually_exclusive(rig, tmp_path, no_batch):
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, "-C", "3", "-u", "--unify-all", "@chan")
    assert exc.value.code == 2


def test_no_valid_channel_ref_with_a_selector_exits_two(rig, tmp_path, no_batch, caplog):
    assert run(tmp_path, "-C", "3", "-t", "not a channel ref") == 2
    assert any("no valid channel" in r.getMessage() for r in caplog.records)


def test_help_no_longer_claims_incompatibility():
    text = cli.build_parser().format_help()
    assert "Incompatible with -c/-t/-r/-u/--unify-all" not in text
    assert "incompatible" not in text.lower()
    assert "-t '@veritasium'" in text  # a pipeline example is shown
