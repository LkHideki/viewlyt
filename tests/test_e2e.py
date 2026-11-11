"""End-to-end tests against real YouTube — a real Chrome + network.

OPT-IN ONLY: every test carries the `e2e` marker and the conftest E2E skipif, so
they are collected but SKIPPED unless VIEWLYT_E2E=1. They tolerate the bot/consent
wall by xfail-ing rather than hard-failing (a flagged IP can't be helped from CI).

    VIEWLYT_E2E=1 uv run pytest -m e2e
"""

from __future__ import annotations

import pytest
from conftest import E2E, VIDEO


@pytest.mark.e2e
@E2E
def test_e2e_scrape_video_smoke():
    import viewlyt

    try:
        r = viewlyt.scrape_video(
            VIDEO,
            comments=True,
            transcript=False,
            limit=5,
            replies=False,
            max_replies=0,
            headless=True,
        )
    except viewlyt.BlockedError as exc:
        pytest.xfail(f"bot/consent wall: {exc.kind}")
    assert r.title.strip()
    assert len(r.top_level) > 0


@pytest.mark.e2e
@E2E
def test_e2e_get_video_views():
    from viewlyt.driver import build_driver
    from viewlyt.scraper import detect_block, dismiss_consent_dialog, get_video_views, safe_get

    driver = build_driver(headless=True)
    try:
        safe_get(driver, VIDEO)
        dismiss_consent_dialog(driver, timeout=2.0)
        if detect_block(driver):
            pytest.xfail("bot/consent wall")
        views = get_video_views(driver)
    finally:
        driver.quit()
    # `get_video_views` returns `int | None`: the exact count, not a rounded "1.2B" label
    # (the " views" text is only rendered later, by `format_view_count`). Rick Astley is
    # well past 1e9, which a rounded or mis-parsed value (e.g. 1.2 -> 1) would not reach.
    assert isinstance(views, int)
    assert views > 1_000_000_000


@pytest.mark.e2e
@E2E
def test_e2e_ytdlp_video_transcript():
    """The browserless transcript path: yt-dlp captions, real network, no Chrome."""
    from viewlyt import ytdl

    r = ytdl.video_transcript("dQw4w9WgXcQ")
    assert r["video_id"] == "dQw4w9WgXcQ"
    assert r["title"].strip()
    assert len(r["segments"]) > 10
    ts, text = r["segments"][0]
    assert ts and text.strip()
    assert isinstance(r["views_int"], int) and r["views_int"] > 1_000_000


@pytest.mark.e2e
@E2E
def test_e2e_cli_transcript_from_the_selenium_panel(tmp_path):
    """The Selenium transcript panel, through the CLI.

    The default CLI e2e now gets its transcript from yt-dlp, so without this the panel
    path (``--transcript-engine selenium``, also the `auto` fallback) has no real-world
    coverage: this one drives real Chrome through "Show transcript"."""
    from viewlyt.cli import main

    out = tmp_path / "out_test"
    rc = main(["-o", str(out), "-q", "--no-tokens", "--transcript-engine", "selenium", "-t", VIDEO])
    files = list(out.glob("*.transcript.md"))
    if rc != 0 or not files:
        pytest.xfail("likely bot/consent wall (no transcript produced)")
    text = files[0].read_text(encoding="utf-8")
    assert files[0].stat().st_size > 0
    assert len(text.splitlines()) > 5  # a real transcript, not a stub
    assert "[0:" not in text  # default output is the token-lean form: stamps stripped


@pytest.mark.e2e
@E2E
def test_e2e_cli_writes_file(tmp_path):
    from viewlyt.cli import main

    out = tmp_path / "out_test"
    rc = main(["-o", str(out), "--limit", "5", "--no-replies", "--max-replies", "0", "-q", VIDEO])
    files = list(out.glob("*.md"))
    if rc != 0 or not files:
        pytest.xfail("likely bot/consent wall (no output produced)")
    assert any(f.stat().st_size > 0 for f in files)
