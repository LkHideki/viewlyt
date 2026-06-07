"""Build a stealth-configured headless/headed Chrome WebDriver.

Selenium 4.6+ ships Selenium Manager, which detects the installed Google Chrome
and downloads/caches the matching ChromeDriver automatically — so there is no
manual ``chromedriver`` to install and no ``webdriver-manager`` dependency.
"""

from __future__ import annotations

import logging
import os
import shutil

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

log = logging.getLogger("viewlyt")

# Override the browser location with this env var (useful on macOS/Windows or for
# Chromium/Brave). When unset, we probe the common Linux path and then PATH, and
# finally fall back to letting Selenium Manager auto-detect the browser.
CHROME_BINARY_ENV = "VIEWLYT_CHROME_BINARY"
_DEFAULT_LINUX_CHROME = "/usr/bin/google-chrome"
_CHROME_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def _resolve_chrome_binary() -> str | None:
    """Locate the Chrome/Chromium binary, or ``None`` to let Selenium auto-detect.

    Priority: ``$VIEWLYT_CHROME_BINARY`` → the common Linux path (if it exists) →
    any chrome/chromium on ``PATH``. Returning ``None`` lets Selenium Manager find
    the browser itself (the standard locations on macOS/Windows), so a plain
    ``pip install viewlyt`` works cross-platform without a hardcoded path.
    """
    env = os.environ.get(CHROME_BINARY_ENV)
    if env:
        return env
    if os.path.exists(_DEFAULT_LINUX_CHROME):
        return _DEFAULT_LINUX_CHROME
    for name in _CHROME_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


# Must match the installed Chrome major version and must NOT contain
# "HeadlessChrome" (a dead giveaway to bot detection).
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

# Injected before any page script runs to file down the most common automation
# tells. Not bullet-proof against advanced fingerprinting, but removes the cheap
# signals (navigator.webdriver, empty plugins/languages, missing window.chrome).
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || { runtime: {} };
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : _origQuery(p)
  );
}
"""


def build_driver(
    headless: bool = True,
    user_data_dir: str | None = None,
    window_size: tuple[int, int] = (1920, 1080),
    page_load_timeout: int = 10,
) -> webdriver.Chrome:
    """Create a configured ``webdriver.Chrome`` instance.

    A real ``--window-size`` is REQUIRED in headless mode: with a zero-size
    viewport YouTube's IntersectionObserver never fires and comments never
    lazy-load.
    """
    opts = Options()
    binary = _resolve_chrome_binary()
    if binary:
        opts.binary_location = binary
        log.debug("using Chrome binary: %s", binary)
    else:
        log.debug("no Chrome binary found on PATH; letting Selenium auto-detect")

    if headless:
        opts.add_argument("--headless=new")

    opts.add_argument(f"--window-size={window_size[0]},{window_size[1]}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={USER_AGENT}")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--accept-lang=en-US,en;q=0.9")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--mute-audio")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")

    if user_data_dir:
        # A persistent, once-logged-in profile is the most reliable way past the
        # "Sign in to confirm you're not a bot" wall on flagged IPs.
        opts.add_argument(f"--user-data-dir={user_data_dir}")

    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # The watch page never truly finishes loading (player/ads keep sockets open),
    # so wait on DOMContentLoaded and drive the rest with explicit waits.
    opts.page_load_strategy = "eager"

    log.info("launching Chrome (headless=%s)", headless)
    driver = webdriver.Chrome(options=opts)

    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})
    except Exception as exc:  # pragma: no cover - CDP may be unavailable
        log.debug("could not install stealth script: %s", exc)

    driver.set_page_load_timeout(page_load_timeout)
    return driver
