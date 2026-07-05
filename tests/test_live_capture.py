"""Server-side capture (managed headless Chrome + snippet): thread lifecycle, CLI.

Drives BrowserCapture with a fake Selenium driver factory — no Chrome, no network.
The fakes raise WebDriverException from find_element so the consent-dialog helper
bails out fast instead of polling its 4s timeout.
"""

from __future__ import annotations

import threading
import time

import pytest
from selenium.common.exceptions import WebDriverException

from viewlyt.live.capture import BrowserCapture, popout_url

SNIPPET = "window.__viewlyt_running = true; /* fake snippet */"
ALIVE_CHECK = "return !!window.__viewlyt_running"


class FakeDriver:
    """Records the Selenium calls BrowserCapture makes; snippet-alive by default."""

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.scripts: list[str] = []
        self.cookies: list[dict] = []
        self.quit_calls = 0
        self.alive_results: list[bool] = []  # queue of _alive() answers; empty -> True

    def get(self, url: str) -> None:
        self.gets.append(url)

    def add_cookie(self, cookie: dict) -> None:
        self.cookies.append(cookie)

    def find_element(self, *a: object, **k: object) -> None:
        raise WebDriverException("no consent dialog in the fake")

    def execute_script(self, script: str, *a: object) -> object:
        self.scripts.append(script)
        if script == ALIVE_CHECK:
            return self.alive_results.pop(0) if self.alive_results else True
        return None

    def quit(self) -> None:
        self.quit_calls += 1


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def test_popout_url_accepts_url_and_bare_id() -> None:
    expected = "https://www.youtube.com/live_chat?is_popout=1&v=dQw4w9WgXcQ"
    assert popout_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == expected
    assert popout_url("dQw4w9WgXcQ") == expected


def test_capture_opens_popout_injects_snippet_and_quits_on_stop() -> None:
    driver = FakeDriver()
    cap = BrowserCapture(popout_url("dQw4w9WgXcQ"), SNIPPET, driver_factory=lambda: driver)
    cap.start()
    _wait_for(lambda: SNIPPET in driver.scripts)

    assert any("live_chat?is_popout=1" in u for u in driver.gets)
    assert any(c.get("name") == "SOCS" for c in driver.cookies)  # consent primed
    # No detour through the (heavy) YouTube home page first — cold-start cost
    # regression guard: the popout is already a .youtube.com origin, so cookies
    # go on right there (see _open_and_inject).
    assert driver.gets == [popout_url("dQw4w9WgXcQ")]

    cap.stop()
    assert driver.quit_calls == 1  # no orphaned Chrome


def test_capture_reinjects_when_the_page_navigated_away() -> None:
    driver = FakeDriver()
    driver.alive_results = [False]  # first health check: snippet gone
    cap = BrowserCapture(
        popout_url("dQw4w9WgXcQ"),
        SNIPPET,
        driver_factory=lambda: driver,
        health_interval=0.01,
    )
    cap.start()
    _wait_for(lambda: driver.scripts.count(SNIPPET) >= 2)
    cap.stop()


def test_capture_rebuilds_the_driver_after_a_crash() -> None:
    drivers: list[FakeDriver] = []
    build_count = 0
    lock = threading.Lock()

    def factory() -> FakeDriver:
        nonlocal build_count
        with lock:
            build_count += 1
            if build_count == 1:
                raise WebDriverException("chrome went away")
            d = FakeDriver()
            drivers.append(d)
            return d

    cap = BrowserCapture(
        popout_url("dQw4w9WgXcQ"),
        SNIPPET,
        driver_factory=factory,
        health_interval=0.01,
        retry_backoff=0.01,
    )
    cap.start()
    _wait_for(lambda: drivers and SNIPPET in drivers[0].scripts)
    cap.stop()
    assert build_count >= 2  # first build failed, the loop retried


# ---------------------------------------------------------------------------
# CLI fail-fast validation
# ---------------------------------------------------------------------------


def test_cli_capture_server_requires_a_url(capsys) -> None:
    from viewlyt.live import cli

    with pytest.raises(SystemExit) as e:
        cli.main(["--capture", "server"])
    assert e.value.code == 2
    assert "needs the live URL" in capsys.readouterr().err
