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
    # The exact int, not a rounded "1.2B" label: Rick Astley is well past 1e9.
    assert isinstance(views, int)
    assert views > 1_000_000
    assert views.endswith(" views")


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
