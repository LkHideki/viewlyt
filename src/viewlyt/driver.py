"""Build a stealth-configured headless/headed Chrome WebDriver.

Selenium 4.6+ ships Selenium Manager, which detects the installed Google Chrome
and downloads/caches the matching ChromeDriver automatically — so there is no
manual ``chromedriver`` to install and no ``webdriver-manager`` dependency.
"""

from __future__ import annotations

import logging
import os
import platform as _platform
import random
import shutil
import sys

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


# ---------------------------------------------------------------------------
# User agent: coherent with the REAL OS and the REAL installed Chrome major,
# lightly rotated per driver.
#
# Why coherence beats a random pool: Chrome also sends Client Hints
# (Sec-CH-UA / Sec-CH-UA-Platform) derived from the real binary — a UA header
# claiming Linux/149 from a macOS Chrome 143 *disagrees with its own hints*,
# which is a stronger bot signal than no spoofing at all. So: platform token
# from the real OS, major from the running binary (read post-launch), and the
# whole story (UA header + metadata) overridden together via CDP.
#
# Why the header is always "{major}.0.0.0": UA Reduction (Chrome 101+) froze
# build/patch to zeros in the header — a real-looking build number there is
# itself a tell. The rotation lives in the MAJOR (current or previous, both
# plausibly in the wild at any time) and is re-drawn for every new driver, so
# a recycled/pooled session comes back with a fresh fingerprint.
# ---------------------------------------------------------------------------
_FALLBACK_CHROME_MAJOR = 143  # used only when the real version can't be read

# sys.platform key -> (UA platform token, Sec-CH platform, platformVersion,
# navigator.platform)
_PLATFORM_SPECS: dict[str, tuple[str, str, str, str]] = {
    "darwin": ("Macintosh; Intel Mac OS X 10_15_7", "macOS", "15.5.0", "MacIntel"),
    "win": ("Windows NT 10.0; Win64; x64", "Windows", "15.0.0", "Win32"),
    "linux": ("X11; Linux x86_64", "Linux", "6.8.0", "Linux x86_64"),
}

# Realistic desktop viewport sizes (all common per display stats). One is drawn
# per driver unless the caller pins window_size — identical fleet-wide windows
# are another cheap automation tell, and any of these keeps YouTube's
# IntersectionObserver lazy-load working.
WINDOW_SIZES: tuple[tuple[int, int], ...] = (
    (1920, 1080),
    (1680, 1050),
    (1600, 900),
    (1536, 864),
    (1440, 900),
)


def _platform_spec() -> tuple[str, str, str, str]:
    if sys.platform == "darwin":
        return _PLATFORM_SPECS["darwin"]
    if sys.platform.startswith("win"):
        return _PLATFORM_SPECS["win"]
    return _PLATFORM_SPECS["linux"]


def pick_user_agent(browser_major: int | None = None) -> tuple[str, str, dict]:
    """Draw a coherent ``(user_agent, navigator_platform, ua_metadata)`` triple.

    ``browser_major`` is the REAL installed Chrome major (falls back to
    ``_FALLBACK_CHROME_MAJOR``); the drawn major is the real one or its
    predecessor. ``ua_metadata`` matches CDP ``Network.setUserAgentOverride``'s
    ``userAgentMetadata`` shape, brands aligned with the UA header. Pure —
    unit-testable without a browser.
    """
    real = browser_major or _FALLBACK_CHROME_MAJOR
    major = random.choice((real, max(real - 1, 100)))
    ua_os, ch_platform, ch_platform_version, nav_platform = _platform_spec()
    version = f"{major}.0.0.0"
    ua = f"Mozilla/5.0 ({ua_os}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
    arch = "arm" if _platform.machine().lower() in ("arm64", "aarch64") else "x86"
    metadata = {
        "brands": [
            {"brand": "Chromium", "version": str(major)},
            {"brand": "Google Chrome", "version": str(major)},
            {"brand": "Not_A Brand", "version": "24"},
        ],
        "fullVersionList": [
            {"brand": "Chromium", "version": version},
            {"brand": "Google Chrome", "version": version},
            {"brand": "Not_A Brand", "version": "24.0.0.0"},
        ],
        "fullVersion": version,
        "platform": ch_platform,
        "platformVersion": ch_platform_version,
        "architecture": arch,
        "bitness": "64",
        "model": "",
        "mobile": False,
        "wow64": False,
    }
    return ua, nav_platform, metadata


# Baseline UA for the Chrome *switch* (pre-launch, no real version known yet):
# platform-coherent, fallback major, no "HeadlessChrome". The CDP override in
# build_driver refines it to the real major + rotation right after launch.
USER_AGENT = (
    f"Mozilla/5.0 ({_platform_spec()[0]}) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{_FALLBACK_CHROME_MAJOR}.0.0.0 Safari/537.36"
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
    window_size: tuple[int, int] | None = None,
    page_load_timeout: int = 10,
    extra_args: tuple[str, ...] = (),
) -> webdriver.Chrome:
    """Create a configured ``webdriver.Chrome`` instance.

    A real ``--window-size`` is REQUIRED in headless mode: with a zero-size
    viewport YouTube's IntersectionObserver never fires and comments never
    lazy-load. ``window_size=None`` (the default) draws one of the realistic
    ``WINDOW_SIZES`` per driver; pass a tuple to pin it. ``extra_args`` appends
    raw Chrome switches for special-purpose drivers (e.g. the live capture
    browser disables the Local Network Access checks that headless can never
    answer a permission prompt for).
    """
    if window_size is None:
        window_size = random.choice(WINDOW_SIZES)
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
    for arg in extra_args:
        opts.add_argument(arg)

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

    # Rotate the coherent fingerprint per driver: real major read from the
    # running binary, UA + Client-Hint metadata overridden TOGETHER so the
    # headers can't contradict each other (see the pick_user_agent block).
    try:
        raw = str((driver.capabilities or {}).get("browserVersion") or "")
        major = int(raw.split(".")[0]) if raw.split(".")[0].isdigit() else None
        ua, nav_platform, metadata = pick_user_agent(major)
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {
                "userAgent": ua,
                "acceptLanguage": "en-US,en;q=0.9",
                "platform": nav_platform,
                "userAgentMetadata": metadata,
            },
        )
        log.debug("user agent for this driver: %s", ua)
    except Exception as exc:  # pragma: no cover - CDP may be unavailable
        log.debug("could not override the user agent via CDP: %s", exc)

    driver.set_page_load_timeout(page_load_timeout)
    return driver
