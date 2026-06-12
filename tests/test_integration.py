"""Integration tests: multi-component orchestration with the Selenium boundary
monkeypatched (no real browser/network).

The boundary functions are patched on the CONSUMER modules (viewlyt.cli /
viewlyt.api), because both bind them at import time via ``from .scraper import ...``
and ``from .driver import build_driver`` — patching viewlyt.scraper/viewlyt.driver
would not rebind the names the code actually calls.
"""

from __future__ import annotations

import pytest
from conftest import FakeDriver, make_scrape_one
from conftest import make_comment as _c
from conftest import make_reply as _r

import viewlyt.api as api
import viewlyt.cli as cli
from viewlyt.cli import format_comment_lines
from viewlyt.htmltext import format_related, format_transcript, slugify
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
        "idA": ("vidA", "Hello World", recs_a, [], []),
        "idB": ("vidB", "Outro", [_c("@x", "hi")], [], []),
    }
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", make_scrape_one(table))

    sums = _run_batch([("vidA", "idA"), ("vidB", "idB")], tmp_path)

    fa = tmp_path / f"{slugify('Hello World')}-vidA.txt"
    assert fa.exists()
    expect = "\n".join(format_comment_lines(recs_a, progress=False, merge_comments=True)) + "\n"
    assert fa.read_text(encoding="utf-8") == expect
    assert [s["video_id"] for s in sums] == ["vidA", "vidB"]
    assert sums[0]["comments"] == 1 and sums[0]["lines"] == 1
    assert sums[0]["error"] is None and sums[0]["segments"] == 0


def test_run_batch_preserves_input_order_under_concurrency(monkeypatch, tmp_path):
    ids = ["v1", "v2", "v3", "v4"]
    table = {f"id{i}": (vid, f"T{vid}", [_c("@a", "x")], [], []) for i, vid in enumerate(ids)}
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", make_scrape_one(table))

    targets = [(vid, f"id{i}") for i, vid in enumerate(ids)]
    sums = _run_batch(targets, tmp_path, jobs=3, expand_replies=False, max_replies=0)
    assert [s["video_id"] for s in sums] == ids


def test_run_batch_merge_comments_threads_into_file(monkeypatch, tmp_path):
    recs = [_c("@joao", "parte1", likes="7"), _r("@maria", "@joao", "resp"), _c("@joao", "parte2")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, [], []))

    merged = _run_batch([("vid", "id")], tmp_path, merge_comments=True)
    body = (tmp_path / f"{slugify('T')}-vid.txt").read_text(encoding="utf-8")
    assert "parte1 parte2" in body and merged[0]["comments"] == 1

    not_merged = _run_batch([("vid", "id")], tmp_path, merge_comments=False)
    assert not_merged[0]["comments"] == 2  # two separate top-level blocks


def test_run_batch_transcript_only_skips_comment_file(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, []))

    sums = _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True)

    plain = tmp_path / f"{slugify('Titulo')}-vid.txt"
    tx = tmp_path / f"{slugify('Titulo')}-vid.transcript.txt"
    assert not plain.exists() and tx.exists()
    assert tx.read_text(encoding="utf-8") == "\n".join(format_transcript(segs)) + "\n"
    assert sums[0]["file"] is None and sums[0]["comments"] == 0
    assert sums[0]["segments"] == 2 and sums[0]["with_transcript"] is True


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
        cli, "scrape_one", lambda d, url, **k: ("vid", "T", [_c("@a", "x")], [], related)
    )

    sums = _run_batch([("vid", "id")], tmp_path, with_related=True, related_limit=5)

    rf = tmp_path / f"{slugify('T')}-vid.related.txt"
    assert rf.exists()
    assert rf.read_text(encoding="utf-8") == "\n".join(format_related(related)) + "\n"
    assert sums[0]["with_related"] is True and sums[0]["related"] == 2
    assert sums[0]["related_file"] == str(rf)
    assert (tmp_path / f"{slugify('T')}-vid.txt").exists()  # comments still written too


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
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", [], [], related))

    sums = _run_batch(
        [("vid", "id")], tmp_path, with_comments=False, with_related=True, related_limit=3
    )

    assert not (tmp_path / f"{slugify('T')}-vid.txt").exists()
    assert (tmp_path / f"{slugify('T')}-vid.related.txt").exists()
    assert sums[0]["file"] is None and sums[0]["comments"] == 0
    assert sums[0]["with_related"] is True and sums[0]["related"] == 1


def test_run_batch_per_video_error_isolation(monkeypatch, tmp_path):
    good = {
        "idA": ("vidA", "A", [_c("@a", "x")], [], []),
        "idC": ("vidC", "C", [_c("@c", "z")], [], []),
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
    assert (tmp_path / f"{slugify('A')}-vidA.txt").exists()
    assert not list(tmp_path.glob("*-vidB.txt"))


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
        return ("vid", "T", [_c("@a", "x")], [], [])

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
    assert sums[0]["error"] == "consent" and calls == [True]


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
    assert kw["with_comments"] is True and kw["with_transcript"] is False
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
# scrape_video (library API)
# --------------------------------------------------------------------------- #
def _patch_api_boundary(monkeypatch, drv, *, block="", title="My Title"):
    """Patch the api-bound Selenium boundary to canned no-ops; return nothing."""
    monkeypatch.setattr(api, "build_driver", lambda **k: drv)
    for name in ("prime_consent_cookies", "safe_get", "dismiss_consent_dialog"):
        monkeypatch.setattr(api, name, lambda *a, **k: None)
    monkeypatch.setattr(api, "detect_block", lambda d: block)
    monkeypatch.setattr(api, "get_video_title", lambda d: title)


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
    assert r.transcript_lines() == format_transcript(segs)
    assert drv.quit_calls == 1


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
