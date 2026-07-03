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
from viewlyt.htmltext import format_related, format_transcript, format_unified, slugify
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
        "idA": ("vidA", "Hello World", recs_a, [], []),
        "idB": ("vidB", "Outro", [_c("@x", "hi")], [], []),
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
    body = (tmp_path / f"{slugify('T')}-vid.md").read_text(encoding="utf-8")
    assert "parte1 parte2" in body and merged[0]["comments"] == 1

    not_merged = _run_batch([("vid", "id")], tmp_path, merge_comments=False)
    assert not_merged[0]["comments"] == 2  # two separate top-level blocks


def test_run_batch_transcript_only_skips_comment_file(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, []))

    sums = _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True)

    plain = tmp_path / f"{slugify('Titulo')}-vid.md"
    tx = tmp_path / f"{slugify('Titulo')}-vid.transcript.md"
    assert not plain.exists() and tx.exists()
    assert tx.read_text(encoding="utf-8") == "\n".join(format_transcript(segs)) + "\n"
    assert sums[0]["file"] is None and sums[0]["comments"] == 0
    assert sums[0]["segments"] == 2 and sums[0]["with_transcript"] is True


def test_run_batch_no_ts_strips_timestamps(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, []))

    _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True, strip_ts=True)

    tx = tmp_path / f"{slugify('Titulo')}-vid.transcript.md"
    assert tx.read_text(encoding="utf-8") == "ola mundo\nsegunda linha\n"


def test_run_batch_copy_puts_output_on_clipboard(monkeypatch, tmp_path):
    segs = [("0:00", "ola mundo"), ("0:02", "segunda linha")]
    monkeypatch.setattr(cli, "build_primed_driver", lambda h, u: FakeDriver())
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "Titulo", [], segs, []))
    grabbed = {}
    monkeypatch.setattr(
        cli, "_copy_to_clipboard", lambda text: grabbed.setdefault("t", text) or True
    )

    _run_batch([("vid", "id")], tmp_path, with_comments=False, with_transcript=True, copy=True)

    # --copy mirrors the produced file's content (single product -> verbatim)
    assert grabbed["t"] == "[0:00] ola mundo\n[0:02] segunda linha"


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
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", [], [], related))

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
    monkeypatch.setattr(cli, "scrape_one", lambda d, url, **k: ("vid", "T", recs, segs, rel))

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
            ("Transcript", format_transcript(segs)),
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
        "idA": ("vidA", "TA", [_c("@a", "xa")], [], []),
        "idB": ("vidB", "TB", [_c("@b", "xb")], [], []),
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


def _scrape_all_three(monkeypatch, *, title="T"):
    """scrape_video mocked to return comments + transcript + related."""
    drv = FakeDriver()
    _patch_api_boundary(monkeypatch, drv, title=title)
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
