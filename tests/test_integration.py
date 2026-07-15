"""Integration tests: multi-component orchestration with the Selenium boundary
monkeypatched (no real browser/network).

The boundary functions are patched on the CONSUMER modules (viewlyt.cli /
viewlyt.api), because both bind them at import time via ``from .scraper import ...``
and ``from .driver import build_driver`` — patching viewlyt.scraper/viewlyt.driver
would not rebind the names the code actually calls.
"""

from __future__ import annotations

import argparse
import contextlib

import pytest
from conftest import FakeDriver, make_scrape_one
from conftest import make_comment as _c
from conftest import make_reply as _r

import viewlyt.api as api
import viewlyt.cli as cli
import viewlyt.snap as snap
import viewlyt.watch as watch
from viewlyt.cli import format_comment_lines
from viewlyt.htmltext import (
    format_related,
    format_transcript,
    format_unified,
    pair_lines,
    slugify,
    strip_timestamps,
)
from viewlyt.scraper import BlockedError


def _run_batch(targets, tmp_path, **overrides):
    """Call run_batch with sensible defaults; override per test."""
    kw = dict(
        jobs=1,
        headless=True,
        fallback=True,
        user_data_dir=None,
        out_dir=str(tmp_path),
        limit=150,
        max_viewports=25,
        expand_replies=True,
        max_replies=5,
        with_comments=True,
        with_transcript=False,
        with_related=False,
        related_limit=0,
        merge_comments=True,
        unify=False,
        unify_all=False,
        inner_progress=False,
        quiet=True,
    )
    kw.update(overrides)
    return cli.run_batch(targets, **kw)


# --------------------------------------------------------------------------- #
# run_batch
# --------------------------------------------------------------------------- #
def test_run_batch_writes_files_with_correct_names_and_counts(monkeypatch, tmp_path):
    recs_a = [_c("@joao", "ola", likes="5", date_raw="2 days ago")]
    table = {
        "idA": ("vidA", "Hello World", recs_a, [], [], ""),
        "idB": ("vidB", "Outro", [_c("@x", "hi")], [], [], ""),
    }
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", make_scrape_one(table))

    sums = _run_batch([("vidA", "idA"), ("vidB", "idB")], tmp_path)

    fa = tmp_path / f"{slugify('Hello World')}-vidA.md"
    assert fa.exists()
    expect = "\n".join(format_comment_lines(recs_a, progress=False, merge_comments=True)) + "\n"
    assert fa.read_text(encoding="utf-8") == expect
    assert [s["video_id"] for s in sums] == ["vidA", "vidB"]
    assert sums[0]["comments"] == 1 and sums[0]["lines"] == 1
    assert sums[0]["error"] is None and sums[0]["segments"] == 0


def test_run_batch_surfaces_views_in_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(
        cli,
        "scrape_one",
        lambda d, url, **k: ("vid", "T", [_c("@a", "x")], [], [], "1,234,567 views"),
    )

    sums = _run_batch([("vid", "id")], tmp_path)
    assert sums[0]["views"] == "1,234,567 views"


def test_run_batch_unify_passes_views_as_subtitle(monkeypatch, tmp_path):
    recs = [_c("@a", "oi", likes="5")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(
        cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, [], [], "42 views")
    )

    _run_batch([("vid", "id")], tmp_path, unify=True, with_comments=True)

    uf = tmp_path / f"{slugify('T')}-vid.unified.md"
    body = uf.read_text(encoding="utf-8")
    expected = format_unified(
        "T",
        [("Comments", format_comment_lines(recs, progress=False, merge_comments=True))],
        subtitle="42 views",
    )
    assert body == "\n".join(expected) + "\n"
    assert "42 views" in body.splitlines()[2]  # right after the title + blank line


def test_run_batch_no_views_omits_subtitle_line(monkeypatch, tmp_path):
    recs = [_c("@a", "oi", likes="5")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, [], [], ""))

    _run_batch([("vid", "id")], tmp_path, unify=True, with_comments=True)

    uf = tmp_path / f"{slugify('T')}-vid.unified.md"
    body = uf.read_text(encoding="utf-8")
    assert body.splitlines()[0] == "# T"
    assert body.splitlines()[1] == ""
    assert body.splitlines()[2] == "## Comments"  # no blank subtitle line inserted


def test_run_batch_preserves_input_order_under_concurrency(monkeypatch, tmp_path):
    ids = ["v1", "v2", "v3", "v4"]
    table = {f"id{i}": (vid, f"T{vid}", [_c("@a", "x")], [], [], "") for i, vid in enumerate(ids)}
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", make_scrape_one(table))

    targets = [(vid, f"id{i}") for i, vid in enumerate(ids)]
    sums = _run_batch(targets, tmp_path, jobs=3, expand_replies=False, max_replies=0)
    assert [s["video_id"] for s in sums] == ids


def test_run_batch_merge_comments_threads_into_file(monkeypatch, tmp_path):
    recs = [_c("@joao", "parte1", likes="7"), _r("@maria", "@joao", "resp"), _c("@joao", "parte2")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, [], [], ""))

    merged = _run_batch([("vid", "id")], tmp_path, merge_comments=True)
    body = (tmp_path / f"{slugify('T')}-vid.md").read_text(encoding="utf-8")
    assert "parte1 parte2" in body and merged[0]["comments"] == 1

    not_merged = _run_batch([("vid", "id")], tmp_path, merge_comments=False)
    assert not_merged[0]["comments"] == 2  # two separate top-level blocks


def test_run_batch_transcript_only_skips_comment_file(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, [], ""))

    sums = _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True)

    plain = tmp_path / f"{slugify('Titulo')}-vid.md"
    tx = tmp_path / f"{slugify('Titulo')}-vid.transcript.md"
    assert not plain.exists() and tx.exists()
    # Default transcript output is token-lean: no [m:ss] stamps, 2 segments/line.
    assert tx.read_text(encoding="utf-8") == "ola mundo segunda linha\n"
    assert sums[0]["file"] is None and sums[0]["comments"] == 0
    assert sums[0]["segments"] == 2 and sums[0]["with_transcript"] is True


def test_run_batch_ts_keeps_timestamps(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha"), ("0:04", "terceira")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, [], ""))

    _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True, with_ts=True)

    tx = tmp_path / f"{slugify('Titulo')}-vid.transcript.md"
    # --ts keeps every stamp; pairing still joins 2 segments per line (odd tail kept).
    assert tx.read_text(encoding="utf-8") == (
        "[0:00] ola mundo [0:02] segunda linha\n[0:04] terceira\n"
    )


def test_run_batch_copy_puts_output_on_clipboard(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, [], ""))
    grabbed = {}
    monkeypatch.setattr(
        cli, "_copy_to_clipboard", lambda text: grabbed.setdefault("t", text) or True
    )

    _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True, copy=True)

    # --copy mirrors the produced file's content (single product -> verbatim;
    # default transcript = no stamps, 2 segments per line)
    assert grabbed["t"] == "ola mundo segunda linha"


def test_run_batch_related_writes_file(monkeypatch, tmp_path):
    related = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel A",
            "views": "1.2B views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        },
        {
            "video_id": "bbbbbbbbbbb",
            "title": "Rel B",
            "views": "20M views",
            "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
        },
    ]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(
        cli, "scrape_one", lambda d, url, **k: ("vid", "T", [_c("@a", "x")], [], related, "")
    )

    sums = _run_batch([("vid", "id")], tmp_path, with_related=True, related_limit=5)

    rf = tmp_path / f"{slugify('T')}-vid.related.md"
    assert rf.exists()
    assert rf.read_text(encoding="utf-8") == "\n".join(format_related(related)) + "\n"
    assert sums[0]["with_related"] is True and sums[0]["related"] == 2
    assert sums[0]["related_file"] == str(rf)
    assert (tmp_path / f"{slugify('T')}-vid.md").exists()  # comments still written too


def test_run_batch_related_only_skips_comment_file(monkeypatch, tmp_path):
    related = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel A",
            "views": "5 views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        }
    ]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", [], [], related, ""))

    sums = _run_batch(
        [("vid", "id")], tmp_path, with_comments=False, with_related=True, related_limit=3
    )

    assert not (tmp_path / f"{slugify('T')}-vid.md").exists()
    assert (tmp_path / f"{slugify('T')}-vid.related.md").exists()
    assert sums[0]["file"] is None and sums[0]["comments"] == 0
    assert sums[0]["with_related"] is True and sums[0]["related"] == 1


def test_run_batch_unify_per_video(monkeypatch, tmp_path):
    recs = [_c("@a", "oi", likes="5")]
    segs = [("0:00", "ola")]
    rel = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel",
            "views": "5 views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        }
    ]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, segs, rel, ""))

    sums = _run_batch(
        [("vid", "id")],
        tmp_path,
        unify=True,
        with_comments=True,
        with_transcript=True,
        with_related=True,
        related_limit=3,
    )

    uf = tmp_path / f"{slugify('T')}-vid.unified.md"
    assert uf.exists()
    # drift guard: the unified file == format_unified over the standalone formatters
    expected = format_unified(
        "T",
        [
            ("Comments", format_comment_lines(recs, progress=False, merge_comments=True)),
            # default transcript treatment applies inside unified too: no stamps, paired
            ("Transcript", pair_lines(strip_timestamps(format_transcript(segs)))),
            ("Related videos", format_related(rel)),
        ],
    )
    assert uf.read_text(encoding="utf-8") == "\n".join(expected) + "\n"
    # A3: the separate per-product files are NOT written
    assert not (tmp_path / f"{slugify('T')}-vid.md").exists()
    assert not (tmp_path / f"{slugify('T')}-vid.transcript.md").exists()
    assert not (tmp_path / f"{slugify('T')}-vid.related.md").exists()
    assert sums[0]["unified_file"] == str(uf)


def test_run_batch_unify_all_global(monkeypatch, tmp_path):
    table = {
        "idA": ("vidA", "TA", [_c("@a", "xa")], [], [], ""),
        "idB": ("vidB", "TB", [_c("@b", "xb")], [], [], ""),
    }
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", make_scrape_one(table))

    sums = _run_batch(
        [("vidA", "idA"), ("vidB", "idB")], tmp_path, jobs=2, unify_all=True, with_comments=True
    )

    gf = tmp_path / "unified-all.md"
    assert gf.exists()
    body = gf.read_text(encoding="utf-8")
    assert body.index("# TA") < body.index("# TB")  # input order preserved
    assert "xa" in body and "xb" in body
    # A2/A3: no per-video files, only the single global one
    assert list(tmp_path.glob("*.md")) == [gf]
    # the global path is stamped on each successful summary
    assert all(s["unified_file"] == str(gf) for s in sums)


def test_run_batch_per_video_error_isolation(monkeypatch, tmp_path):
    good = {
        "idA": ("vidA", "A", [_c("@a", "x")], [], [], ""),
        "idC": ("vidC", "C", [_c("@c", "z")], [], [], ""),
    }

    def scrape_one(driver, url, **kw):
        if url == "idB":
            raise RuntimeError("boom")
        return good[url]

    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", scrape_one)

    targets = [("vidA", "idA"), ("vidB", "idB"), ("vidC", "idC")]
    sums = _run_batch(targets, tmp_path, expand_replies=False, max_replies=0)
    by = {s["video_id"]: s for s in sums}
    assert by["vidB"]["error"] == "boom"
    assert by["vidA"]["error"] is None and by["vidC"]["error"] is None
    assert (tmp_path / f"{slugify('A')}-vidA.md").exists()
    assert not list(tmp_path.glob("*-vidB.md"))


def test_run_batch_blocked_retries_headed_when_fallback_true(monkeypatch, tmp_path):
    headless_calls = []

    def build(headless, user_data_dir):
        headless_calls.append(headless)
        return FakeDriver()

    state = {"n": 0}

    def scrape_one(driver, url, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise BlockedError("consent")
        return ("vid", "T", [_c("@a", "x")], [], [], "")

    monkeypatch.setattr(cli, "build_primed_driver", build)
    monkeypatch.setattr(cli, "scrape_one", scrape_one)

    sums = _run_batch([("vid", "id")], tmp_path, expand_replies=False, max_replies=0)
    assert sums[0]["error"] is None
    assert headless_calls == [True, False]  # rebuilt headed on the fallback


def test_run_batch_blocked_not_retried_when_fallback_false(monkeypatch, tmp_path):
    calls = []

    def build(headless, user_data_dir):
        calls.append(headless)
        return FakeDriver()

    def scrape_one(driver, url, **kw):
        raise BlockedError("consent")

    monkeypatch.setattr(cli, "build_primed_driver", build)
    monkeypatch.setattr(cli, "scrape_one", scrape_one)

    sums = _run_batch(
        [("vid", "id")], tmp_path, fallback=False, expand_replies=False, max_replies=0
    )
    # The generic once-per-video retry still runs (fresh session, SAME mode) but
    # with fallback=False the worker must never rebuild headed.
    assert sums[0]["error"] == "consent"
    assert calls and all(headless is True for headless in calls)


def test_run_batch_retries_failed_video_once(monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky(driver, url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return ("vid", "T", [_c("@a", "oi")], [], [], "")

    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", flaky)

    sums = _run_batch([("vid", "id")], tmp_path, expand_replies=False, max_replies=0)
    # transient first failure -> re-queued once on a fresh session -> success
    assert calls["n"] == 2
    assert len(sums) == 1 and sums[0]["error"] is None
    assert (tmp_path / f"{slugify('T')}-vid.md").exists()


def test_run_batch_gives_up_after_second_failure(monkeypatch, tmp_path):
    calls = {"n": 0}

    def always_fails(driver, url, **kw):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", always_fails)

    sums = _run_batch([("vid", "id")], tmp_path, expand_replies=False, max_replies=0)
    # exactly ONE retry: 2 attempts total, a single (failed) summary, no dupes
    assert calls["n"] == 2
    assert len(sums) == 1 and sums[0]["error"] == "boom"


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def test_main_success_returns_zero_and_wires_modes(monkeypatch, capsys):
    captured = {}

    def fake_run_batch(targets, **kw):
        captured["targets"] = targets
        captured["kw"] = kw
        return [
            {
                "url": "u",
                "video_id": "dQw4w9WgXcQ",
                "title": "T",
                "error": None,
                "with_comments": True,
                "file": "f",
                "comments": 3,
                "lines": 5,
                "with_transcript": False,
                "transcript_file": None,
                "segments": 0,
            }
        ]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    rc = cli.main(["dQw4w9WgXcQ"])
    assert rc == 0
    assert captured["targets"] == [("dQw4w9WgXcQ", "dQw4w9WgXcQ")]
    kw = captured["kw"]
    # no selector -> transcript-only is the default
    assert kw["with_comments"] is False and kw["with_transcript"] is True
    assert kw["headless"] is True and kw["fallback"] is True and kw["jobs"] == 1
    assert "Done: 1/1" in capsys.readouterr().out


def test_main_transcript_flag_wiring(monkeypatch):
    cap = {}

    def fake_run_batch(targets, **kw):
        cap["targets"] = targets
        cap["kw"] = kw
        return [
            {
                "url": "u",
                "video_id": t[0],
                "error": None,
                "with_comments": False,
                "file": None,
                "comments": 0,
                "lines": 0,
                "with_transcript": True,
                "transcript_file": "tf",
                "segments": 2,
            }
            for t in targets
        ]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    rc = cli.main(["-t", "dQw4w9WgXcQ", "abcdefghij_", "--jobs", "2"])
    assert rc == 0
    assert cap["kw"]["with_comments"] is False and cap["kw"]["with_transcript"] is True
    assert cap["kw"]["jobs"] == 2 and len(cap["targets"]) == 2


def test_main_related_flag_wiring(monkeypatch):
    cap = {}

    def fake_run_batch(targets, **kw):
        cap["kw"] = kw
        return [
            {
                "video_id": t[0],
                "error": None,
                "with_comments": False,
                "file": None,
                "comments": 0,
                "lines": 0,
                "with_transcript": False,
                "transcript_file": None,
                "segments": 0,
                "with_related": True,
                "related_file": "rf",
                "related": 5,
            }
            for t in targets
        ]

    monkeypatch.setattr(cli, "run_batch", fake_run_batch)
    rc = cli.main(["-r", "5", "dQw4w9WgXcQ"])
    assert rc == 0
    assert cap["kw"]["with_related"] is True and cap["kw"]["related_limit"] == 5
    assert cap["kw"]["with_comments"] is False  # -r alone -> related only


def _fake_run_batch_capturing(cap, summary_extra):
    def fake_run_batch(targets, **kw):
        cap["kw"] = kw
        return [
            {
                "video_id": t[0],
                "error": None,
                "comments": 1,
                "lines": 1,
                "segments": 1,
                "related": 1,
            }
            | summary_extra
            for t in targets
        ]

    return fake_run_batch


def test_main_unify_alone_collects_all(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        cli,
        "run_batch",
        _fake_run_batch_capturing(
            cap,
            {
                "with_comments": True,
                "with_transcript": True,
                "with_related": True,
                "unified_file": "uf",
            },
        ),
    )
    rc = cli.main(["--unify", "dQw4w9WgXcQ"])
    assert rc == 0
    kw = cap["kw"]
    assert kw["unify"] is True and kw["unify_all"] is False
    assert kw["with_comments"] and kw["with_transcript"] and kw["with_related"]
    assert kw["related_limit"] == 20  # _UNIFY_DEFAULT_RELATED, since no -r given


def test_main_unify_with_r_keeps_collect_all_just_sets_count(monkeypatch):
    # -r N is NOT a product selector for --unify: it still collects everything,
    # only overriding the related count (regression for the run-real bug).
    cap = {}
    monkeypatch.setattr(
        cli,
        "run_batch",
        _fake_run_batch_capturing(
            cap,
            {
                "with_comments": True,
                "with_transcript": True,
                "with_related": True,
                "unified_file": "uf",
            },
        ),
    )
    rc = cli.main(["--unify", "-r", "3", "dQw4w9WgXcQ"])
    assert rc == 0
    kw = cap["kw"]
    assert kw["with_comments"] and kw["with_transcript"] and kw["with_related"]
    assert kw["related_limit"] == 3  # -r overrides the count but keeps collect-all


def test_main_unify_respects_explicit_selectors(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        cli,
        "run_batch",
        _fake_run_batch_capturing(
            cap,
            {
                "with_comments": True,
                "with_transcript": True,
                "with_related": False,
                "unified_file": "uf",
            },
        ),
    )
    rc = cli.main(["-c", "-t", "--unify", "dQw4w9WgXcQ"])
    assert rc == 0
    kw = cap["kw"]
    assert kw["unify"] is True
    assert kw["with_comments"] and kw["with_transcript"] and not kw["with_related"]
    assert kw["related_limit"] == 0  # NOT forced when selectors are given


def test_main_unify_all_wiring(monkeypatch):
    cap = {}
    monkeypatch.setattr(
        cli,
        "run_batch",
        _fake_run_batch_capturing(
            cap,
            {
                "with_comments": True,
                "with_transcript": True,
                "with_related": True,
                "unified_file": "unified-all.md",
            },
        ),
    )
    rc = cli.main(["--unify-all", "dQw4w9WgXcQ", "abcdefghij_"])
    assert rc == 0
    assert cap["kw"]["unify_all"] is True and cap["kw"]["unify"] is False
    assert cap["kw"]["related_limit"] == 20


def test_main_all_failed_returns_one(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "run_batch", lambda targets, **kw: [{"video_id": "dQw4w9WgXcQ", "error": "consent"}]
    )
    rc = cli.main(["dQw4w9WgXcQ"])
    assert rc == 1
    out = capsys.readouterr()
    assert "consent" in out.err and "Done: 0/1" in out.out


def test_main_no_valid_urls_returns_two(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_batch must not be called")

    monkeypatch.setattr(cli, "run_batch", boom)
    assert cli.main(["not-a-url"]) == 2
    assert cli.main([]) == 2


def test_main_unreadable_from_file_returns_two(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("run_batch must not be called")

    monkeypatch.setattr(cli, "run_batch", boom)
    assert cli.main(["--from-file", str(tmp_path / "nope.csv")]) == 2


# --------------------------------------------------------------------------- #
# -C/--channel (run_channel_batch + main's early branch)
# --------------------------------------------------------------------------- #
def _channel_items(n=3, prefix="v"):
    return [
        {
            "video_id": f"{prefix}{i}",
            "title": f"Video {i}",
            "views": f"{i * 1000:,} views",
            "url": f"https://www.youtube.com/watch?v={prefix}{i}",
        }
        for i in range(1, n + 1)
    ]


def test_run_channel_batch_yt_dlp_writes_file(monkeypatch, tmp_path):
    items = _channel_items(3)
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("Some Channel", items))

    def boom(*a, **k):
        raise AssertionError("selenium fallback must not be called when yt-dlp succeeds")

    monkeypatch.setattr(cli, "build_primed_driver", boom)
    monkeypatch.setattr(cli, "collect_channel_videos", boom)

    sums = cli.run_channel_batch(
        ["@someone"],
        n=3,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    assert len(sums) == 1
    s = sums[0]
    assert s["error"] is None
    assert s["engine"] == "yt-dlp"
    assert s["channel"] == "Some Channel"
    assert s["count"] == 3
    assert s["order"] == "recent"

    out_path = tmp_path / f"{slugify('Some Channel')}.channel.md"
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8") == "\n".join(format_related(items)) + "\n"
    assert s["channel_file"] == str(out_path)


def test_run_channel_batch_falls_back_to_selenium_when_ytdl_raises(monkeypatch, tmp_path):
    items = _channel_items(2, prefix="s")

    def raise_ytdl(ref, n, order):
        raise RuntimeError("yt-dlp boom")

    monkeypatch.setattr(cli.ytdl, "channel_entries", raise_ytdl)
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "collect_channel_videos", lambda d, ref, limit, order, progress: items)

    sums = cli.run_channel_batch(
        ["@someone"],
        n=2,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    s = sums[0]
    assert s["error"] is None
    assert s["engine"] == "selenium"
    assert s["count"] == 2


def test_run_channel_batch_falls_back_when_ytdl_returns_empty(monkeypatch, tmp_path):
    items = _channel_items(1, prefix="e")
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "collect_channel_videos", lambda d, ref, limit, order, progress: items)

    sums = cli.run_channel_batch(
        ["@someone"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    s = sums[0]
    assert s["error"] is None
    assert s["engine"] == "selenium"
    assert s["count"] == 1


def test_run_channel_batch_popular_always_routes_to_selenium(monkeypatch, tmp_path):
    # ytdl.channel_entries(order="popular") always raises NotImplementedError
    # (YouTube rejects the PU... playlist) — the fallback must still work.
    items = _channel_items(1, prefix="p")

    def raise_not_implemented(ref, n, order):
        raise NotImplementedError("no supported way to list popular uploads")

    monkeypatch.setattr(cli.ytdl, "channel_entries", raise_not_implemented)
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "collect_channel_videos", lambda d, ref, limit, order, progress: items)

    sums = cli.run_channel_batch(
        ["@someone"],
        n=1,
        order="popular",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    s = sums[0]
    assert s["error"] is None
    assert s["engine"] == "selenium"
    assert s["order"] == "popular"


def test_run_channel_batch_both_engines_fail_sets_error_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "collect_channel_videos", lambda d, ref, limit, order, progress: [])

    sums = cli.run_channel_batch(
        ["@nobody"],
        n=5,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    s = sums[0]
    assert s["error"] is not None
    assert s["channel_file"] is None
    assert list(tmp_path.iterdir()) == []


def test_run_channel_batch_isolates_driver_build_failure(monkeypatch, tmp_path):
    # A Chrome/driver build failure (e.g. SessionNotCreatedException) must not
    # escape run_channel_batch — each ref gets its own error, not a crash.
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))

    def boom(h, u):
        raise RuntimeError("chrome missing")

    monkeypatch.setattr(cli, "build_primed_driver", boom)

    sums = cli.run_channel_batch(
        ["@channel-a", "@channel-b"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    assert len(sums) == 2
    for s in sums:
        assert s["error"] is not None
        assert s["channel_file"] is None
    assert list(tmp_path.iterdir()) == []


def test_run_channel_batch_recovers_after_collect_failure_on_earlier_ref(monkeypatch, tmp_path):
    # A dead shared session mid-batch (collect_channel_videos raising) must
    # not poison later refs — the driver is rebuilt and later channels succeed.
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))

    built = []

    def fake_build(h, u):
        drv = FakeDriver()
        built.append(drv)
        return drv

    monkeypatch.setattr(cli, "build_primed_driver", fake_build)

    def fake_collect(d, ref, limit, order, progress):
        if ref == "@channel-a":
            raise RuntimeError("session died")
        return _channel_items(1, prefix=ref)

    monkeypatch.setattr(cli, "collect_channel_videos", fake_collect)

    sums = cli.run_channel_batch(
        ["@channel-a", "@channel-b"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    assert len(sums) == 2
    a, b = sums
    assert a["error"] is not None
    assert a["channel_file"] is None
    assert b["error"] is None
    assert b["channel_file"] is not None
    assert (tmp_path / f"{slugify('@channel-b'.lstrip('@'))}.channel.md").exists()
    # The dead driver from @channel-a was quit and a fresh one built for @channel-b.
    assert len(built) == 2
    assert built[0].quit_calls == 1


def test_run_channel_batch_shares_one_driver_across_channels(monkeypatch, tmp_path):
    built = []

    def fake_build(h, u):
        drv = FakeDriver()
        built.append(drv)
        return drv

    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))
    monkeypatch.setattr(cli, "build_primed_driver", fake_build)
    monkeypatch.setattr(
        cli,
        "collect_channel_videos",
        lambda d, ref, limit, order, progress: _channel_items(1, prefix=ref),
    )

    sums = cli.run_channel_batch(
        ["@a", "@b", "@c"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        quiet=True,
    )
    assert len(sums) == 3
    assert all(s["engine"] == "selenium" and s["error"] is None for s in sums)
    # Only ONE driver built for all 3 channels (lazy + shared), quit exactly once.
    assert len(built) == 1
    assert built[0].quit_calls == 1


def test_run_channel_batch_no_fallback_errors_without_building_driver(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("", []))

    def boom(*a, **k):
        raise AssertionError("driver must not be built when fallback=False")

    monkeypatch.setattr(cli, "build_primed_driver", boom)

    sums = cli.run_channel_batch(
        ["@someone"],
        n=1,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=False,
        user_data_dir=None,
        quiet=True,
    )
    s = sums[0]
    assert s["error"] is not None
    assert s["channel_file"] is None


def test_run_channel_batch_copy_puts_output_on_clipboard(monkeypatch, tmp_path):
    items = _channel_items(2)
    monkeypatch.setattr(cli.ytdl, "channel_entries", lambda ref, n, order: ("Chan", items))
    copied = {}
    monkeypatch.setattr(
        cli, "_copy_to_clipboard", lambda text: copied.setdefault("text", text) or True
    )

    cli.run_channel_batch(
        ["@someone"],
        n=2,
        order="recent",
        out_dir=str(tmp_path),
        headless=True,
        fallback=True,
        user_data_dir=None,
        copy=True,
        quiet=True,
    )
    assert "\n".join(format_related(items)) in copied["text"]


def test_main_channel_flag_dispatches_to_run_channel_batch(monkeypatch, capsys):
    cap = {}

    def fake_run_channel_batch(refs, **kw):
        cap["refs"] = refs
        cap["kw"] = kw
        return [
            {
                "ref": "@someone",
                "channel": "Someone",
                "count": 10,
                "order": kw["order"],
                "engine": "yt-dlp",
                "channel_file": "out/someone.channel.md",
                "error": None,
            }
        ]

    monkeypatch.setattr(cli, "run_channel_batch", fake_run_channel_batch)
    rc = cli.main(["-C", "10", "@someone"])
    assert rc == 0
    assert cap["refs"] == ["@someone"]
    assert cap["kw"]["n"] == 10 and cap["kw"]["order"] == "recent"
    out = capsys.readouterr().out
    assert "Done: 1/1 channel(s)" in out
    assert "10 videos" in out


def test_main_channel_order_propagated(monkeypatch):
    cap = {}

    def fake_run_channel_batch(refs, **kw):
        cap["kw"] = kw
        return []

    monkeypatch.setattr(cli, "run_channel_batch", fake_run_channel_batch)
    cli.main(["-C", "5", "--order", "old", "@someone"])
    assert cap["kw"]["order"] == "old"


def test_main_channel_multiple_refs(monkeypatch):
    cap = {}

    def fake_run_channel_batch(refs, **kw):
        cap["refs"] = refs
        return [
            {
                "ref": r,
                "channel": r,
                "count": 1,
                "order": "recent",
                "engine": "yt-dlp",
                "channel_file": f"out/{r}.channel.md",
                "error": None,
            }
            for r in refs
        ]

    monkeypatch.setattr(cli, "run_channel_batch", fake_run_channel_batch)
    rc = cli.main(["-C", "1", "@channel-a", "@channel-b"])
    assert rc == 0
    assert cap["refs"] == ["@channel-a", "@channel-b"]


@pytest.mark.parametrize("flag", [["-c"], ["-t"], ["-r", "5"], ["-u"], ["--unify-all"]])
def test_main_channel_incompatible_with_other_selectors_exits_two(monkeypatch, flag):
    def boom(*a, **k):
        raise AssertionError("run_channel_batch must not be called")

    monkeypatch.setattr(cli, "run_channel_batch", boom)
    monkeypatch.setattr(cli, "run_batch", boom)
    rc = cli.main(["-C", "5", *flag, "@someone"])
    assert rc == 2


def test_main_channel_no_valid_refs_returns_two(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("run_channel_batch must not be called")

    monkeypatch.setattr(cli, "run_channel_batch", boom)
    assert cli.main(["-C", "5", "not a channel ref at all"]) == 2


def test_main_order_without_channel_is_warning_no_op(monkeypatch, caplog):
    cap = {}

    def fake_run_batch(targets, **kw):
        cap["called"] = True
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
    rc = cli.main(["--order", "old", "dQw4w9WgXcQ"])
    assert rc == 0
    assert cap.get("called") is True


def test_channel_file_key_included_in_token_report(monkeypatch, tmp_path, capsys):
    pytest.importorskip("tiktoken")
    f = tmp_path / "chan.channel.md"
    f.write_text("1. [Title](url)\n", encoding="utf-8")
    monkeypatch.setattr(cli.tokens, "tiktoken_available", lambda: True)
    cli._print_token_report(
        [{"channel_file": str(f)}],
        budget_tokens=1000,
    )
    out = capsys.readouterr().out
    assert str(f) in out


def test_gather_channel_refs_dedupes_and_skips_invalid(caplog):
    refs = cli.gather_channel_refs(
        ["@veritasium", "https://www.youtube.com/@veritasium/videos", "not a channel"], []
    )
    assert refs == ["@veritasium"]


def test_gather_channel_refs_from_file(tmp_path):
    f = tmp_path / "channels.txt"
    f.write_text("@channel-a\n@channel-b\n@channel-a\n", encoding="utf-8")
    refs = cli.gather_channel_refs([], [str(f)])
    assert refs == ["@channel-a", "@channel-b"]


# --------------------------------------------------------------------------- #
# scrape_video (library API)
# --------------------------------------------------------------------------- #
def _patch_api_boundary(monkeypatch, drv, *, block="", title="My Title", views=""):
    """Patch the api-bound Selenium boundary to canned no-ops; return nothing."""
    monkeypatch.setattr(api, "build_driver", lambda **k: drv)
    for name in ("prime_consent_cookies", "safe_get", "dismiss_consent_dialog"):
        monkeypatch.setattr(api, name, lambda *a, **k: None)
    monkeypatch.setattr(api, "detect_block", lambda d: block)
    monkeypatch.setattr(api, "get_video_title", lambda d: title)
    monkeypatch.setattr(api, "get_video_views", lambda d: views)


def test_scrape_video_returns_structured_result(monkeypatch):
    drv = FakeDriver()
    recs = [
        {
            "kind": "comment",
            "author": "@joao",
            "html": "<b>oi</b>",
            "likes": "5",
            "date_raw": "2 days ago",
        },
        {
            "kind": "reply",
            "author": "@maria",
            "parent_author": "@joao",
            "html": "resp",
            "likes": "1",
            "date_raw": "1 day ago",
        },
    ]
    segs = [("0:00", "ola"), ("0:02", "mundo")]
    _patch_api_boundary(monkeypatch, drv)
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: recs)
    monkeypatch.setattr(api, "fetch_transcript", lambda d, **k: segs)

    r = api.scrape_video("dQw4w9WgXcQ", comments=True, transcript=True)
    assert r.video_id == "dQw4w9WgXcQ" and r.title == "My Title"
    assert (
        len(r.top_level) == 1 and r.top_level[0].author == "@joao" and r.top_level[0].text == "oi"
    )
    assert len(r.replies) == 1 and r.replies[0].parent_author == "@joao"
    # default: token-lean (no [m:ss], 2 segments/line); verbatim form still available
    assert r.transcript_lines() == ["ola mundo"]
    assert r.transcript_lines(timestamps=True, pair=False) == format_transcript(segs)
    assert drv.quit_calls == 1


def test_scrape_video_surfaces_views(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T", views="1,234,567 views")
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: [])

    r = api.scrape_video("dQw4w9WgXcQ", comments=True)
    assert r.views == "1,234,567 views"


def test_scrape_video_views_default_empty(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T")  # views="" default
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: [])

    r = api.scrape_video("dQw4w9WgXcQ", comments=True)
    assert r.views == ""


def test_scrape_video_quits_driver_on_error(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv)

    def boom(d):
        raise RuntimeError("mid-scrape")

    monkeypatch.setattr(api, "get_video_title", boom)
    with pytest.raises(RuntimeError, match="mid-scrape"):
        api.scrape_video("dQw4w9WgXcQ")
    assert drv.quit_calls == 1


def test_scrape_video_blocked_propagates(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, block="consent")

    def unreached(d, **k):
        raise AssertionError("collect_comments must not be reached")

    monkeypatch.setattr(api, "collect_comments", unreached)
    with pytest.raises(BlockedError) as ei:
        api.scrape_video("dQw4w9WgXcQ")
    assert ei.value.kind == "consent" and drv.quit_calls == 1


def test_scrape_video_comments_only_skips_transcript(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T")
    monkeypatch.setattr(
        api,
        "collect_comments",
        lambda d, **k: [
            {"kind": "comment", "author": "@a", "html": "x", "likes": "0", "date_raw": ""}
        ],
    )

    def unreached(d, **k):
        raise AssertionError("transcript must be skipped")

    monkeypatch.setattr(api, "fetch_transcript", unreached)
    r = api.scrape_video("dQw4w9WgXcQ", comments=True, transcript=False)
    assert r.transcript == [] and r.transcript_lines() == [] and len(r.top_level) == 1


def test_scrape_video_collects_related(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T")
    rel = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel A",
            "views": "1.2B views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        }
    ]
    monkeypatch.setattr(api, "collect_related", lambda d, **k: rel)

    def unreached(d, **k):
        raise AssertionError("comments/transcript must be skipped")

    monkeypatch.setattr(api, "collect_comments", unreached)
    monkeypatch.setattr(api, "fetch_transcript", unreached)

    r = api.scrape_video("dQw4w9WgXcQ", comments=False, related=5)
    assert len(r.related) == 1
    assert r.related[0].video_id == "aaaaaaaaaaa" and r.related[0].views == "1.2B views"
    assert r.related_lines() == [
        "1. [1.2B views. Rel A](https://www.youtube.com/watch?v=aaaaaaaaaaa)"
    ]
    assert r.comments == [] and r.transcript == [] and drv.quit_calls == 1


def test_scrape_video_related_zero_skips(monkeypatch):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T")
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: [])

    def unreached(d, **k):
        raise AssertionError("collect_related must be skipped when related=0")

    monkeypatch.setattr(api, "collect_related", unreached)
    r = api.scrape_video("dQw4w9WgXcQ", comments=True, related=0)
    assert r.related == [] and r.related_lines() == []


def test_scrape_result_comment_lines_and_write(monkeypatch, tmp_path):
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="Meu Título")
    recs = [
        {
            "kind": "comment",
            "author": "@joao",
            "html": "<b>oi</b>",
            "likes": "5",
            "date_raw": "2 days ago",
        },
        {
            "kind": "reply",
            "author": "@maria",
            "parent_author": "@joao",
            "html": "resp",
            "likes": "1",
            "date_raw": "1 day ago",
        },
    ]
    segs = [("0:00", "ola")]
    rel = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel",
            "views": "5 views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        }
    ]
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: recs)
    monkeypatch.setattr(api, "fetch_transcript", lambda d, **k: segs)
    monkeypatch.setattr(api, "collect_related", lambda d, **k: rel)

    r = api.scrape_video("dQw4w9WgXcQ", comments=True, transcript=True, related=3)

    # comment_lines() reproduces the CLI .md body byte-for-byte
    assert r.comment_lines() == format_comment_lines(recs, progress=False, merge_comments=True)

    # write() drops exactly the three non-empty files with the CLI's names
    written = r.write(str(tmp_path))
    slug = slugify("Meu Título")
    assert set(written) == {"comments", "transcript", "related"}
    assert written["comments"] == tmp_path / f"{slug}-dQw4w9WgXcQ.md"
    assert (tmp_path / f"{slug}-dQw4w9WgXcQ.transcript.md").exists()
    assert (tmp_path / f"{slug}-dQw4w9WgXcQ.related.md").exists()
    assert written["comments"].read_text(encoding="utf-8") == "\n".join(r.comment_lines()) + "\n"


def test_scrape_result_write_skips_empty_sections(monkeypatch, tmp_path):
    # Only non-empty sections are written (no 0-byte files).
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title="T")
    monkeypatch.setattr(
        api,
        "collect_comments",
        lambda d, **k: [
            {"kind": "comment", "author": "@a", "html": "x", "likes": "0", "date_raw": ""}
        ],
    )
    r = api.scrape_video("dQw4w9WgXcQ", comments=True, transcript=False, related=0)
    written = r.write(str(tmp_path))
    assert set(written) == {"comments"}  # no transcript/related files
    assert list(tmp_path.glob("*.md")) == [tmp_path / f"{slugify('T')}-dQw4w9WgXcQ.md"]


def _scrape_all_three(monkeypatch, *, title="T", views=""):
    """scrape_video mocked to return comments + transcript + related."""
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title=title, views=views)
    recs = [
        {
            "kind": "comment",
            "author": "@a",
            "html": "<b>oi</b>",
            "likes": "5",
            "date_raw": "2 days ago",
        },
        {
            "kind": "reply",
            "author": "@b",
            "parent_author": "@a",
            "html": "re",
            "likes": "0",
            "date_raw": "",
        },
    ]
    segs = [("0:00", "ola"), ("0:02", "mundo")]
    rel = [
        {
            "video_id": "aaaaaaaaaaa",
            "title": "Rel",
            "views": "5 views",
            "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        }
    ]
    monkeypatch.setattr(api, "collect_comments", lambda d, **k: recs)
    monkeypatch.setattr(api, "fetch_transcript", lambda d, **k: segs)
    monkeypatch.setattr(api, "collect_related", lambda d, **k: rel)
    return api.scrape_video("dQw4w9WgXcQ", comments=True, transcript=True, related=3)


def test_scrape_result_unified_lines_matches_standalone(monkeypatch):
    # Drift guard: the unified doc MUST be exactly format_unified() over the
    # standalone formatters, in canonical order — pins ordering/skip-empty and
    # keeps api/cli section enumerations honest.
    r = _scrape_all_three(monkeypatch, title="Meu Título")
    expected = format_unified(
        "Meu Título",
        [
            ("Comments", r.comment_lines()),
            ("Transcript", r.transcript_lines()),
            ("Related videos", r.related_lines()),
        ],
    )
    assert r.unified_lines() == expected
    # and each standalone section's lines appear contiguously inside it
    assert r.comment_lines()[0] in r.unified_lines()
    assert "## Comments" in r.unified_lines() and "## Related videos" in r.unified_lines()


def test_scrape_result_unified_lines_passes_views_as_subtitle(monkeypatch):
    r = _scrape_all_three(monkeypatch, title="T", views="1,234,567 views")
    assert r.views == "1,234,567 views"
    expected = format_unified(
        "T",
        [
            ("Comments", r.comment_lines()),
            ("Transcript", r.transcript_lines()),
            ("Related videos", r.related_lines()),
        ],
        subtitle="1,234,567 views",
    )
    assert r.unified_lines() == expected
    assert r.unified_lines()[2] == "1,234,567 views"


def test_scrape_result_write_unify_replaces_separate_files(monkeypatch, tmp_path):
    r = _scrape_all_three(monkeypatch, title="T")
    written = r.write(str(tmp_path), unify=True)

    uf = tmp_path / f"{slugify('T')}-dQw4w9WgXcQ.unified.md"
    assert set(written) == {"unified"} and written["unified"] == uf
    assert uf.read_text(encoding="utf-8") == "\n".join(r.unified_lines()) + "\n"
    # A3: the separate per-product files are NOT written alongside the unified one
    assert not (tmp_path / f"{slugify('T')}-dQw4w9WgXcQ.md").exists()
    assert not (tmp_path / f"{slugify('T')}-dQw4w9WgXcQ.transcript.md").exists()
    assert not (tmp_path / f"{slugify('T')}-dQw4w9WgXcQ.related.md").exists()
    assert list(tmp_path.glob("*.md")) == [uf]


# --------------------------------------------------------------------------- #
# Session / scrape_videos (reused-browser library API)
# --------------------------------------------------------------------------- #
def _patch_session_boundary(monkeypatch, *, on_safe_get=None, on_detect=None):
    """Patch the api boundary for Session/scrape_videos tests, EXCEPT build_driver
    (each test controls that to observe driver reuse / count)."""
    for name in ("prime_consent_cookies", "dismiss_consent_dialog"):
        monkeypatch.setattr(api, name, lambda *a, **k: None)
    monkeypatch.setattr(api, "safe_get", on_safe_get or (lambda *a, **k: None))
    monkeypatch.setattr(api, "detect_block", on_detect or (lambda d: ""))
    monkeypatch.setattr(api, "get_video_title", lambda d: "T")
    monkeypatch.setattr(api, "get_video_views", lambda d: "")
    monkeypatch.setattr(
        api,
        "collect_comments",
        lambda d, **k: [
            {"kind": "comment", "author": "@a", "html": "x", "likes": "0", "date_raw": ""}
        ],
    )


def test_session_reuses_one_driver_across_videos(monkeypatch):
    builds = []

    def fake_build(**kw):
        d = FakeDriver()
        builds.append(d)
        return d

    monkeypatch.setattr(api, "build_driver", fake_build)
    _patch_session_boundary(monkeypatch)

    with api.Session(headless=True) as s:
        a = s.scrape("dQw4w9WgXcQ", comments=True)
        b = s.scrape("abcdefghij_", comments=True)

    assert len(builds) == 1  # ONE Chrome built and reused across both videos
    assert a.video_id == "dQw4w9WgXcQ" and b.video_id == "abcdefghij_"
    assert builds[0].quit_calls == 1  # closed once on context-manager exit


def test_session_fallback_rebuilds_headed_on_block(monkeypatch):
    builds = []

    def fake_build(*, headless, user_data_dir):
        builds.append(headless)
        return FakeDriver()

    state = {"n": 0}

    def detect(d):
        state["n"] += 1
        return "consent" if state["n"] == 1 else ""

    monkeypatch.setattr(api, "build_driver", fake_build)
    _patch_session_boundary(monkeypatch, on_detect=detect)

    with api.Session(headless=True, fallback=True) as s:
        r = s.scrape("dQw4w9WgXcQ")
    assert builds == [True, False]  # rebuilt headed after the block, retried
    assert r.video_id == "dQw4w9WgXcQ"


def test_session_fallback_false_reraises(monkeypatch):
    monkeypatch.setattr(api, "build_driver", lambda **k: FakeDriver())
    _patch_session_boundary(monkeypatch, on_detect=lambda d: "consent")
    with api.Session(headless=True, fallback=False) as s:
        with pytest.raises(BlockedError) as ei:
            s.scrape("dQw4w9WgXcQ")
    assert ei.value.kind == "consent"


def test_scrape_videos_preserves_order_and_isolates_failures(monkeypatch):
    monkeypatch.setattr(api, "build_driver", lambda **k: FakeDriver())

    def safe_get(driver, watch_url):
        if "abcdefghij_" in watch_url:  # make the 2nd video fail mid-scrape
            raise RuntimeError("boom")

    _patch_session_boundary(monkeypatch, on_safe_get=safe_get)

    urls = ["dQw4w9WgXcQ", "abcdefghij_", "kLmNoPqRsTu"]
    results = api.scrape_videos(urls, jobs=2)

    assert len(results) == 3  # aligned to input order
    assert results[1] is None  # the failing video -> None, not dropped
    assert results[0].video_id == "dQw4w9WgXcQ"
    assert results[2].video_id == "kLmNoPqRsTu"


def test_scrape_videos_empty_returns_empty(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("build_driver must not be called for an empty list")

    monkeypatch.setattr(api, "build_driver", boom)
    assert api.scrape_videos([]) == []


# --------------------------------------------------------------------------- #
# vl watch
# --------------------------------------------------------------------------- #
def test_watch_run_daemon_stops_at_max_count(monkeypatch, tmp_path):
    values = iter(["https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/TgMJUAo-tWA"])
    monkeypatch.setattr(watch, "read_clipboard", lambda: next(values, None))
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)

    args = argparse.Namespace(max_count=2, timeout=0, poll_interval=0, quiet=True)
    queue = watch._run_daemon(args, tmp_path / "queue.json")
    assert [item["video_id"] for item in queue] == ["dQw4w9WgXcQ", "TgMJUAo-tWA"]


def test_watch_run_daemon_stops_at_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "read_clipboard", lambda: None)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    clock = iter([0.0, 0.0, 5.0])  # start; first check (not yet); second check (timed out)
    monkeypatch.setattr(watch.time, "monotonic", lambda: next(clock))

    args = argparse.Namespace(max_count=0, timeout=3.0, poll_interval=0, quiet=True)
    queue = watch._run_daemon(args, tmp_path / "queue.json")
    assert queue == []


def test_watch_main_no_clipboard_tool_exits_two(monkeypatch, tmp_path, capsys):
    def boom():
        raise RuntimeError("no clipboard read tool found (pbpaste/powershell/xclip/xsel)")

    monkeypatch.setattr(watch, "read_clipboard", boom)
    rc = watch.main(["--queue-file", str(tmp_path / "q.json"), "--poll-interval", "0"])
    assert rc == 2
    assert "no clipboard read tool found" in capsys.readouterr().err


def test_watch_list_prints_saved_queue(tmp_path, capsys):
    qf = tmp_path / "queue.json"
    watch._save_queue(qf, [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}])
    rc = watch.main(["--queue-file", str(qf), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "aaaaaaaaaaa" in out and "u1" in out


def test_watch_list_on_empty_queue(tmp_path, capsys):
    rc = watch.main(["--queue-file", str(tmp_path / "q.json"), "--list"])
    assert rc == 0
    assert "vazia" in capsys.readouterr().out


def test_watch_drop_last_removes_and_persists(tmp_path):
    qf = tmp_path / "queue.json"
    watch._save_queue(
        qf,
        [
            {"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"},
            {"video_id": "bbbbbbbbbbb", "url": "u2", "added_at": "y"},
        ],
    )
    rc = watch.main(["--queue-file", str(qf), "--drop-last"])
    assert rc == 0
    assert [i["video_id"] for i in watch._load_queue(qf)] == ["aaaaaaaaaaa"]


def test_watch_drop_last_on_empty_queue_is_noop(tmp_path, capsys):
    qf = tmp_path / "queue.json"
    rc = watch.main(["--queue-file", str(qf), "--drop-last"])
    assert rc == 0
    assert "vazia" in capsys.readouterr().err
    assert not qf.exists()


def test_watch_review_tui_deselect_then_run(monkeypatch):
    queue = [
        {"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"},
        {"video_id": "bbbbbbbbbbb", "url": "u2", "added_at": "y"},
    ]
    keys = iter(["down", "space", "enter"])  # move to item 2, deselect it, run item 1
    monkeypatch.setattr(watch, "_read_key", lambda: next(keys))
    monkeypatch.setattr(watch, "_raw_terminal", contextlib.nullcontext)

    to_run = watch._review_tui(queue)
    assert [i["video_id"] for i in to_run] == ["aaaaaaaaaaa"]
    assert [i["video_id"] for i in queue] == ["bbbbbbbbbbb"]  # deselected item stays queued


def test_watch_review_tui_quit_leaves_queue_untouched(monkeypatch):
    queue = [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}]
    monkeypatch.setattr(watch, "_read_key", lambda: "quit")
    monkeypatch.setattr(watch, "_raw_terminal", contextlib.nullcontext)

    assert watch._review_tui(queue) is None
    assert len(queue) == 1


def test_watch_review_tui_cursor_wraps_and_toggles_correct_item(monkeypatch):
    queue = [
        {"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"},
        {"video_id": "bbbbbbbbbbb", "url": "u2", "added_at": "y"},
        {"video_id": "ccccccccccc", "url": "u3", "added_at": "z"},
    ]
    # up from cursor 0 wraps to the last item; deselect it; run the rest.
    keys = iter(["up", "space", "enter"])
    monkeypatch.setattr(watch, "_read_key", lambda: next(keys))
    monkeypatch.setattr(watch, "_raw_terminal", contextlib.nullcontext)

    to_run = watch._review_tui(queue)
    assert [i["video_id"] for i in to_run] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert [i["video_id"] for i in queue] == ["ccccccccccc"]


def test_watch_run_and_yes_dispatches_to_cli_main(monkeypatch, tmp_path):
    qf = tmp_path / "queue.json"
    watch._save_queue(
        qf,
        [
            {"video_id": "aaaaaaaaaaa", "url": "https://youtu.be/aaaaaaaaaaa", "added_at": "x"},
            {"video_id": "bbbbbbbbbbb", "url": "https://youtu.be/bbbbbbbbbbb", "added_at": "y"},
        ],
    )
    captured = {}
    monkeypatch.setattr(cli, "main", lambda argv: captured.__setitem__("argv", argv) or 0)

    rc = watch.main(["--queue-file", str(qf), "--run", "--yes", "-c", "-o", str(tmp_path)])
    assert rc == 0
    assert captured["argv"] == [
        "https://youtu.be/aaaaaaaaaaa",
        "https://youtu.be/bbbbbbbbbbb",
        "-c",
        "--out-dir",
        str(tmp_path),
    ]
    assert watch._load_queue(qf) == []  # queue emptied after a successful dispatch


def test_watch_quiet_flag_forwarded_to_dispatch(monkeypatch, tmp_path):
    qf = tmp_path / "queue.json"
    watch._save_queue(qf, [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}])
    captured = {}
    monkeypatch.setattr(cli, "main", lambda argv: captured.__setitem__("argv", argv) or 0)

    watch.main(["--queue-file", str(qf), "--run", "--yes", "--quiet", "-o", str(tmp_path)])
    assert "--quiet" in captured["argv"]


def test_watch_run_without_yes_requires_a_real_tty(monkeypatch, tmp_path):
    """Under pytest neither stdin nor stdout is a real tty — the raw-mode TUI
    can't run, so it must fail loud (pointing at --yes) instead of hanging."""
    qf = tmp_path / "queue.json"
    watch._save_queue(qf, [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}])

    def unreached(argv):
        raise AssertionError("cli.main must not be called without a tty/--yes")

    monkeypatch.setattr(cli, "main", unreached)
    rc = watch.main(["--queue-file", str(qf), "--run", "-o", str(tmp_path)])
    assert rc == 2
    assert watch._load_queue(qf) == [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}]


def test_watch_run_without_yes_uses_review_tui(monkeypatch, tmp_path):
    qf = tmp_path / "queue.json"
    watch._save_queue(
        qf,
        [
            {"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"},
            {"video_id": "bbbbbbbbbbb", "url": "u2", "added_at": "y"},
        ],
    )
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(watch.sys.stdout, "isatty", lambda: True)
    keys = iter(["down", "space", "enter"])  # deselect item 2, run item 1 only
    monkeypatch.setattr(watch, "_read_key", lambda: next(keys))
    monkeypatch.setattr(watch, "_raw_terminal", contextlib.nullcontext)
    captured = {}
    monkeypatch.setattr(cli, "main", lambda argv: captured.__setitem__("argv", argv) or 0)

    rc = watch.main(["--queue-file", str(qf), "--run", "-o", str(tmp_path)])
    assert rc == 0
    assert captured["argv"][0] == "u1"
    assert watch._load_queue(qf) == [{"video_id": "bbbbbbbbbbb", "url": "u2", "added_at": "y"}]


def test_watch_run_without_yes_quit_does_not_dispatch(monkeypatch, tmp_path):
    qf = tmp_path / "queue.json"
    watch._save_queue(qf, [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}])
    monkeypatch.setattr(watch.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(watch.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(watch, "_read_key", lambda: "quit")
    monkeypatch.setattr(watch, "_raw_terminal", contextlib.nullcontext)

    def unreached(argv):
        raise AssertionError("cli.main must not be called when the review is quit")

    monkeypatch.setattr(cli, "main", unreached)

    rc = watch.main(["--queue-file", str(qf), "--run", "-o", str(tmp_path)])
    assert rc == 0
    assert watch._load_queue(qf) == [{"video_id": "aaaaaaaaaaa", "url": "u1", "added_at": "x"}]


def test_watch_empty_queue_never_dispatches(monkeypatch, tmp_path):
    qf = tmp_path / "queue.json"

    def unreached(argv):
        raise AssertionError("cli.main must not be called for an empty queue")

    monkeypatch.setattr(cli, "main", unreached)
    monkeypatch.setattr(watch, "read_clipboard", lambda: None)
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)
    clock = iter([0.0, 5.0])  # start; first check already past the timeout
    monkeypatch.setattr(watch.time, "monotonic", lambda: next(clock))

    rc = watch.main(["--queue-file", str(qf), "--timeout", "1", "--yes"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# vl snap (viewlyt.snap.main), patched at snap.ytdl.* / snap.copy_to_clipboard
# --------------------------------------------------------------------------- #
def test_snap_video_writes_file_and_prints(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        snap.ytdl,
        "video_info",
        lambda vid: {"title": "Why Machines Learn", "channel": "Veritasium", "view_count": 100},
    )

    rc = snap.main(["dQw4w9WgXcQ", "-o", str(tmp_path)])
    assert rc == 0

    out_file = tmp_path / "why-machines-learn-dQw4w9WgXcQ.snapshot.md"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "# Why Machines Learn" in content
    assert "- Channel: Veritasium" in content

    captured = capsys.readouterr()
    assert "Why Machines Learn" in captured.out


def test_snap_channel_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        snap.ytdl,
        "channel_info",
        lambda ref, latest=5: {
            "channel": "Veritasium",
            "uploader_id": "@veritasium",
            "entries": [],
        },
    )

    rc = snap.main(["@veritasium", "-o", str(tmp_path)])
    assert rc == 0

    out_file = tmp_path / "veritasium.snapshot.md"
    assert out_file.exists()
    assert "# Veritasium" in out_file.read_text(encoding="utf-8")


def test_snap_channel_ref_tried_before_video_id(monkeypatch, tmp_path):
    """A channel URL must route to channel_info, never video_info."""
    calls = []
    monkeypatch.setattr(
        snap.ytdl,
        "channel_info",
        lambda ref, latest=5: calls.append(("channel", ref)) or {"channel": "C", "entries": []},
    )
    monkeypatch.setattr(
        snap.ytdl,
        "video_info",
        lambda vid: calls.append(("video", vid)) or {"title": "T"},
    )

    rc = snap.main(["https://www.youtube.com/@veritasium", "-o", str(tmp_path), "-q"])
    assert rc == 0
    assert calls == [("channel", "@veritasium")]


def test_snap_invalid_target_exits_two(tmp_path, capsys):
    rc = snap.main(["just some prose", "-o", str(tmp_path)])
    assert rc == 2
    assert "vl snap:" in capsys.readouterr().err


def test_snap_download_error_exits_one(monkeypatch, tmp_path, capsys):
    def boom(vid):
        raise RuntimeError("Video unavailable")

    monkeypatch.setattr(snap.ytdl, "video_info", boom)

    rc = snap.main(["dQw4w9WgXcQ", "-o", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "vl snap:" in err
    assert "Video unavailable" in err
    assert not (tmp_path / "dQw4w9WgXcQ.snapshot.md").exists()


def test_snap_copy_puts_output_on_clipboard(monkeypatch, tmp_path):
    monkeypatch.setattr(snap.ytdl, "video_info", lambda vid: {"title": "T", "view_count": 5})
    copied = {}
    monkeypatch.setattr(
        snap, "copy_to_clipboard", lambda text: copied.setdefault("text", text) or True
    )

    rc = snap.main(["dQw4w9WgXcQ", "-o", str(tmp_path), "--copy"])
    assert rc == 0
    assert "# T" in copied["text"]


def test_snap_quiet_suppresses_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(snap.ytdl, "video_info", lambda vid: {"title": "T"})

    rc = snap.main(["dQw4w9WgXcQ", "-o", str(tmp_path), "-q"])
    assert rc == 0
    assert capsys.readouterr().out == ""
