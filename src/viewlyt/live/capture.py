"""Server-side live-chat capture: a managed headless Chrome runs the popout + snippet.

``--capture server`` exists because Safari/WebKit blocks insecure ``ws://`` from
https pages for EVERY host — loopback included (verified empirically against
WebKit 26.5: the handshake never leaves the browser) — so a Safari user can never
run the capture snippet in their own browser. Instead of asking them to, the
server drives its OWN headless Chrome to the chat popout and injects the same
snippet the Capture panel ships; from there the pipeline is identical
(snippet → ws://127.0.0.1/ingest → queue → worker).

Why not a plain HTTP chat library: the one candidate (chat-downloader, last
released Sep/2023) can no longer parse today's YouTube — verified against a real
live ("Unable to parse initial video data") — and hand-rolling the InnerTube
protocol would add a second scraping surface to maintain. Chrome + Selenium are
already hard requirements of the VOD scraper, and the snippet is already proven
inside Chrome, so this path reuses both.

Selenium is imported lazily (the module stays importable without it, mirroring
how live/llm.py defers openai), and every Selenium call happens on ONE dedicated
daemon thread — WebDriver is not thread-safe.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("viewlyt.live")

# Chrome gates https-page → loopback connections behind a "local network access"
# permission PROMPT — which headless can never show, so it silently denies and the
# snippet's WebSocket dies on handshake (verified: badge stuck on "disconnected,
# retrying"). This capture Chrome only ever opens the chat popout, so disabling
# the check is contained; the spellings cover the feature's renames across
# Chrome versions.
_PNA_DISABLE_FLAGS = (
    "--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks,"
    "PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults",
)


def popout_url(video_url_or_id: str) -> str:
    """The chat-popout URL for a video URL/id (the page the snippet targets)."""
    from ..scraper import extract_video_id

    vid = extract_video_id(video_url_or_id)
    return f"https://www.youtube.com/live_chat?is_popout=1&v={vid}"


class BrowserCapture:
    """Owns the capture thread and its Chrome; ``stop()`` shuts both down cleanly.

    ``driver_factory`` (a zero-arg callable) is injectable for tests; production
    uses :func:`viewlyt.driver.build_driver` (headless, stealth-configured, with
    the Local Network Access checks disabled — see ``_PNA_DISABLE_FLAGS``).
    """

    def __init__(
        self,
        page: str,
        snippet: str,
        *,
        driver_factory=None,
        health_interval: float = 2.0,
        retry_backoff: float = 5.0,
    ) -> None:
        self._page = page
        self._snippet = snippet
        self._driver_factory = driver_factory
        self._health_interval = health_interval
        self._retry_backoff = retry_backoff
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="viewlyt-live-capture", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Signal the thread and wait for it to quit its Chrome (no orphans)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    # -- internals (all Selenium happens on the capture thread) ---------------

    def _build(self):
        if self._driver_factory is not None:
            return self._driver_factory()
        from ..driver import build_driver

        return build_driver(headless=True, extra_args=_PNA_DISABLE_FLAGS)

    def _open_and_inject(self, driver) -> None:
        from ..scraper import dismiss_consent_dialog, prime_consent_cookies, safe_get

        prime_consent_cookies(driver)
        safe_get(driver, self._page)
        dismiss_consent_dialog(driver)
        driver.execute_script(self._snippet)

    def _alive(self, driver) -> bool:
        return bool(driver.execute_script("return !!window.__viewlyt_running"))

    def _run(self) -> None:
        driver = None
        while not self._stop.is_set():
            try:
                if driver is None:
                    driver = self._build()
                    self._open_and_inject(driver)
                    logger.info("server-side capture attached to %s", self._page)
                elif not self._alive(driver):
                    # The page navigated or reloaded: the snippet is gone, re-inject.
                    driver.execute_script(self._snippet)
            except Exception:
                logger.exception(
                    "server-side capture hiccup (is Google Chrome installed?); "
                    "rebuilding the browser in %ss",
                    self._retry_backoff,
                )
                driver = self._quit(driver)
                self._stop.wait(self._retry_backoff)
                continue
            self._stop.wait(self._health_interval)
        self._quit(driver)

    @staticmethod
    def _quit(driver):
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        return None
