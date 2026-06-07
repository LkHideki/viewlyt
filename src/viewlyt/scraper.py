"""Navigation, block-bypass, and the load/expand/harvest of YouTube comments.

All Selenium WebDriver interaction lives here and runs on a single thread —
WebDriver instances are NOT thread-safe. The CPU-light HTML->text conversion is
what gets parallelised later (see :mod:`viewlyt.cli`).

Harvesting is two-phase to keep loading and expansion from fighting each other:

* **Phase A (load)** scrolls to the bottom repeatedly to lazy-load up to
  ``limit`` top-level comment threads (capped by ``max_viewports`` scrolls).
* **Phase B (expand + harvest)** walks each thread exactly once: scrolls it into
  view, clicks "Read more" to un-truncate the text, expands replies with a
  *trusted* click (a plain JS ``.click()`` does not trigger YouTube's reply
  fetch), then records the comment and its replies with author + like count.

Processing each thread once (rather than re-harvesting on every scroll) also
removes the duplicate-line artifact that incremental harvesting produced.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, urlparse

from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tqdm import tqdm

log = logging.getLogger("viewlyt")

COMMENTS_CONTAINER = "ytd-comments#comments"
COMMENT_THREAD = "ytd-comment-thread-renderer"
READ_MORE = "#more"  # "Read more" text expander (not #more-replies)
MORE_REPLIES = "#more-replies"  # "View N replies" toggle
REPLIES_RENDERER = "ytd-comment-replies-renderer"

# Each per-field selector is an ORDERED fallback list: the first one that yields a
# value wins, so collection survives YouTube renaming/wrapping a node (a single
# pinned selector would silently return "" -> the field comes out empty/unknown).
# The legacy scalar names below stay defined as the first element for readability
# and for anyone importing them.
TOP_COMMENT_SELECTORS = (
    "#comment",  # the top-level comment inside a thread
    "ytd-comment-view-model#comment",
    "#comment-content",
)
COMMENT_TEXT_SELECTORS = (
    "#content-text",  # canonical; tag-agnostic (yt-attributed/formatted-string)
    "yt-attributed-string#content-text",
    "#comment-content #content-text",
    "#content #content-text",
    "yt-formatted-string#content-text",
)
COMMENT_AUTHOR_SELECTORS = (
    "#author-text",
    "a#author-text",
    "#header-author #author-text",
    "#author-comment-badge #author-text",  # channel-owner / badge case
    "h3 #author-text",
)
LIKES_SELECTORS = (
    "#vote-count-middle",  # like count (empty when zero)
    "#vote-count-left",
    "[id*=vote-count]",  # last-ditch any vote-count node
)
PUBLISHED_TIME_SELECTORS = (
    "#published-time-text",  # relative timestamp, e.g. "2 days ago"
    "#published-time-text a",
    "a.yt-simple-endpoint#published-time-text",
    "#header-author #published-time-text",
)
TOP_COMMENT = TOP_COMMENT_SELECTORS[0]
COMMENT_TEXT = COMMENT_TEXT_SELECTORS[0]
COMMENT_AUTHOR = COMMENT_AUTHOR_SELECTORS[0]
LIKES = LIKES_SELECTORS[0]
PUBLISHED_TIME = PUBLISHED_TIME_SELECTORS[0]

REPLY_ITEM = (
    "ytd-comment-replies-renderer ytd-comment-view-model, "
    "ytd-comment-replies-renderer ytd-comment-renderer, "
    "#replies ytd-comment-view-model, "  # wrapper id instead of renderer tag
    "#replies ytd-comment-renderer, "
    "#loaded-replies ytd-comment-view-model"  # some layouts use #loaded-replies
)
# Used only when REPLY_ITEM finds nothing after a successful expand: any non-top-level
# comment node directly under the thread.
REPLY_ITEM_FALLBACK = (
    "ytd-comment-view-model:not([is-top-level]), ytd-comment-renderer:not(#comment)"
)
REPLY_CONTINUATION = "ytd-comment-replies-renderer ytd-continuation-item-renderer"

# Localized "comments are turned off" markers — lets the load phase bail in <1s
# instead of waiting out the whole first-thread timeout on a disabled section.
COMMENTS_OFF_MARKERS = (
    "comments are turned off",
    "comentários estão desativados",
    "comentários foram desativados",
    "los comentarios están desactivados",
)

# Transcript (description -> "Show transcript" -> engagement panel)
DESC_EXPAND = "#description-inline-expander #expand, tp-yt-paper-button#expand"
TRANSCRIPT_SECTION = "ytd-video-description-transcript-section-renderer"
TRANSCRIPT_PANEL = (
    'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"]'
)
# YouTube's "modern transcript" panel renders each line as
# <transcript-segment-view-model> (timestamp in a .ytw…Timestamp div, text in a
# .ytAttributedStringHost span); older layouts used <ytd-transcript-segment-renderer>
# with .segment-timestamp/.segment-text. Match BOTH so the scraper survives the
# rollout (and any clients still served the legacy UI).
#
# Each modern segment also has a *sibling* .ytwTranscriptSegmentViewModelTimestampA11yLabel
# div holding the screen-reader spoken form ("30 minutes, 40 seconds"). The timestamp
# selector must therefore stay an EXACT class-token match — never a substring/startswith
# on "ytwTranscriptSegmentViewModelTimestamp" — or it would also grab that A11y label.
TRANSCRIPT_SEGMENT = "transcript-segment-view-model, ytd-transcript-segment-renderer"
TRANSCRIPT_TS = ".ytwTranscriptSegmentViewModelTimestamp, .segment-timestamp"
TRANSCRIPT_SEG_TEXT = "span.ytAttributedStringHost, yt-formatted-string.segment-text, .segment-text"

# Locale-dependent "before you continue" consent buttons (best-effort fallback;
# the cookie priming below usually skips the interstitial entirely).
CONSENT_XPATHS = [
    "//button[@aria-label='Accept all']",
    "//button[@aria-label='Aceitar tudo']",
    "//button[contains(@class,'VfPpkd-LgbsSe') and ("
    "@aria-label='Accept all' or @aria-label='Aceitar tudo' or "
    "@aria-label='Reject all' or @aria-label='Rejeitar tudo')]",
    "//*[self::button or self::a][contains(.,'Accept all') or contains(.,'Aceitar tudo')]",
]

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_ID_RE = re.compile(r"/(?:shorts|embed|v|live)/([A-Za-z0-9_-]{11})")
_ANY_ID_RE = re.compile(r"([A-Za-z0-9_-]{11})")

_DEFAULT_TIMEOUT_NOTE = 10  # seconds; matches build_driver's page_load_timeout


class BlockedError(RuntimeError):
    """Raised when YouTube serves a consent wall or bot-check instead of the page."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


# --------------------------------------------------------------------------- #
# URL / navigation helpers
# --------------------------------------------------------------------------- #
def extract_video_id(url: str) -> str:
    """Extract the 11-char video id from any common YouTube URL form."""
    url = (url or "").strip()
    if _VIDEO_ID_RE.match(url):
        return url

    parsed = urlparse(url if "//" in url else "https://" + url)
    host = (parsed.hostname or "").lower()

    if host.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(candidate):
            return candidate

    if "youtube" in host:
        qs = parse_qs(parsed.query)
        if "v" in qs and _VIDEO_ID_RE.match(qs["v"][0]):
            return qs["v"][0]
        m = _PATH_ID_RE.search(parsed.path)
        if m:
            return m.group(1)

    m = _ANY_ID_RE.search(url)
    if m:
        return m.group(1)

    raise ValueError(f"Could not extract a YouTube video id from: {url!r}")


def safe_get(driver, url: str) -> None:
    """``driver.get`` with the page-load timeout caught: stop loading and carry
    on with whatever DOM is present (the watch page never fully settles)."""
    try:
        driver.get(url)
    except TimeoutException:
        log.warning(
            "page load exceeded %ss for %s — stopping load and continuing",
            _DEFAULT_TIMEOUT_NOTE,
            url,
        )
        try:
            driver.execute_script("window.stop();")
        except WebDriverException:
            pass


def prime_consent_cookies(driver) -> None:
    """Pre-set consent cookies so the interstitial is skipped on fresh profiles."""
    try:
        safe_get(driver, "https://www.youtube.com/")
        for cookie in (
            {"name": "SOCS", "value": "CAI", "domain": ".youtube.com", "path": "/"},
            {"name": "CONSENT", "value": "YES+", "domain": ".youtube.com", "path": "/"},
        ):
            try:
                driver.add_cookie(cookie)
            except WebDriverException as exc:
                log.debug("add_cookie(%s) failed: %s", cookie["name"], exc)
    except WebDriverException as exc:
        log.warning("could not prime consent cookies: %s", exc)


def dismiss_consent_dialog(driver, timeout: float = 4.0) -> bool:
    """Best-effort click of a cookie-consent button if one is shown."""
    combined = " | ".join(CONSENT_XPATHS)
    try:
        btn = WebDriverWait(driver, timeout).until(EC.element_to_be_clickable((By.XPATH, combined)))
        btn.click()
        log.info("dismissed consent dialog")
        time.sleep(1.0)
        return True
    except (TimeoutException, WebDriverException):
        return False


def get_video_title(driver) -> str:
    """Return the video title (for the output filename slug).

    Prefers the ``og:title`` meta tag (clean, present in <head> right after
    DOMContentLoaded), falling back to ``document.title`` with the trailing
    " - YouTube" suffix and any "(N)" notification-count prefix stripped.
    """
    try:
        title = (
            driver.execute_script(
                "var m = document.querySelector('meta[property=\"og:title\"]');"
                "return (m && m.content) ? m.content : (document.title || '');"
            )
            or ""
        )
    except WebDriverException:
        title = ""
    title = re.sub(r"\s*-\s*YouTube\s*$", "", title)
    title = re.sub(r"^\(\d+\)\s*", "", title)
    return title.strip()


def detect_block(driver) -> str | None:
    """Return 'consent'/'botwall' if YouTube blocked us, else None."""
    try:
        url = driver.current_url or ""
    except WebDriverException:
        url = ""
    if "consent." in url:
        return "consent"
    try:
        src = (driver.page_source or "").lower()
    except WebDriverException:
        return None
    if "sign in to confirm" in src or "not a bot" in src:
        return "botwall"
    return None


def _comments_disabled(driver) -> bool:
    """True when YouTube shows a 'comments are turned off' notice for this video.

    Reads only the comments section's text (not the whole page) and never raises —
    on any WebDriver error it returns False so the caller falls through to its
    normal wait/timeout path.
    """
    try:
        src = (
            driver.execute_script(
                "var c=document.querySelector('ytd-comments,#comments,#sections');"
                "return c ? c.innerText : '';"
            )
            or ""
        ).lower()
    except WebDriverException:
        return False
    return any(marker in src for marker in COMMENTS_OFF_MARKERS)


# --------------------------------------------------------------------------- #
# Element helpers
# --------------------------------------------------------------------------- #
def _text(el, css: str) -> str:
    """Text of a descendant via ``textContent`` (collapsed whitespace).

    Deliberately NOT Selenium's ``.text``: that returns only *rendered/visible*
    text, so it yields "" for nodes that are off-viewport or visually hidden —
    e.g. the plain ``#author-text`` of a channel-owner comment, whose name is
    shown in a highlighted badge instead (which made such authors come out as
    ``unknown``). ``textContent`` reads the name regardless of visibility.
    """
    try:
        node = el.find_element(By.CSS_SELECTOR, css)
        return " ".join((node.get_attribute("textContent") or "").split())
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return ""


def _inner_html(el, css: str) -> str:
    try:
        return el.find_element(By.CSS_SELECTOR, css).get_attribute("innerHTML") or ""
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return ""


def _first_text(el, selectors) -> str:
    """``textContent`` of the first matching descendant among ``selectors`` (in order).

    Delegates to :func:`_text` (inheriting its exception-swallowing), so it never
    raises; returns "" when none of the selectors match a non-empty node.
    """
    for css in selectors:
        val = _text(el, css)
        if val:
            return val
    return ""


def _first_inner_html(el, selectors) -> str:
    """``innerHTML`` of the first descendant among ``selectors`` with non-blank content.

    Skips whitespace-only matches so an empty wrapper can't shadow a populated
    alternate; returns "" when none match.
    """
    for css in selectors:
        html = _inner_html(el, css)
        if html.strip():
            return html
    return ""


def _likes(comment_el) -> str:
    """Like count text (e.g. '842', '1.2K'); '0' when the count is empty/hidden."""
    return _first_text(comment_el, LIKES_SELECTORS) or "0"


def _top_el(thread):
    for css in TOP_COMMENT_SELECTORS:
        try:
            return thread.find_element(By.CSS_SELECTOR, css)
        except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
            continue
    return thread


def _scroll_into_view(driver, el) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
    except (StaleElementReferenceException, WebDriverException):
        pass


def _safe_click(driver, el) -> bool:
    """Centre the element and click it with a trusted Selenium click (needed for
    YouTube's reply toggles), falling back to a JS click if intercepted."""
    _scroll_into_view(driver, el)
    try:
        el.click()
        return True
    except (ElementClickInterceptedException, StaleElementReferenceException, WebDriverException):
        try:
            driver.execute_script("arguments[0].click();", el)
            return True
        except (StaleElementReferenceException, WebDriverException):
            return False


def _click_read_more(driver, comment_el) -> None:
    """Expand a truncated comment ("Read more" / "...mais") if the button shows."""
    try:
        btn = comment_el.find_element(By.CSS_SELECTOR, READ_MORE)
        if btn.is_displayed():
            driver.execute_script("arguments[0].click();", btn)
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass


def _expand_replies(driver, thread, max_replies: int, max_more_clicks: int = 20) -> None:
    """Expand a thread's replies up to ``max_replies``: click "View N replies",
    then keep clicking the "Show more replies" continuation until enough replies
    are loaded or it is gone (also bounded by ``max_more_clicks``)."""
    if max_replies <= 0:
        return
    try:
        toggle = thread.find_element(By.CSS_SELECTOR, MORE_REPLIES)
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        return  # this comment has no replies

    if not _safe_click(driver, toggle):
        return
    try:
        WebDriverWait(driver, 6).until(
            lambda d: len(thread.find_elements(By.CSS_SELECTOR, REPLY_ITEM)) > 0
        )
    except (TimeoutException, StaleElementReferenceException, WebDriverException):
        return

    for _ in range(max_more_clicks):
        try:
            before = len(thread.find_elements(By.CSS_SELECTOR, REPLY_ITEM))
        except (StaleElementReferenceException, WebDriverException):
            break
        if before >= max_replies:
            break  # already loaded enough replies for this comment
        try:
            conts = thread.find_elements(By.CSS_SELECTOR, REPLY_CONTINUATION)
        except (StaleElementReferenceException, WebDriverException):
            break
        cont = next((c for c in conts if _displayed(c)), None)
        if cont is None:
            break
        target = cont
        try:
            btns = cont.find_elements(By.CSS_SELECTOR, "button, tp-yt-paper-button")
            if btns:
                target = btns[0]
        except (StaleElementReferenceException, WebDriverException):
            pass
        if not _safe_click(driver, target):
            break
        try:
            WebDriverWait(driver, 5).until(
                lambda d, before=before: (
                    len(thread.find_elements(By.CSS_SELECTOR, REPLY_ITEM)) > before
                )
            )
        except (TimeoutException, StaleElementReferenceException, WebDriverException):
            break


def _displayed(el) -> bool:
    try:
        return el.is_displayed()
    except (StaleElementReferenceException, WebDriverException):
        return False


def _scroll_for_more(driver) -> None:
    """Trigger the next comment continuation. Bringing the LAST loaded thread's
    bottom (where the loading spinner lives) to the viewport edge re-arms
    YouTube's IntersectionObserver far more reliably than ``scrollTo(bottom)``
    alone, then we still jump to the document bottom for good measure."""
    driver.execute_script(
        "var t=document.querySelectorAll(arguments[0]);"
        "if(t.length){t[t.length-1].scrollIntoView({block:'end'});}"
        "window.scrollTo(0, document.documentElement.scrollHeight);",
        COMMENT_THREAD,
    )


# --------------------------------------------------------------------------- #
# Two-phase collection
# --------------------------------------------------------------------------- #
def collect_comments(
    driver,
    limit: int = 100,
    max_viewports: int = 25,
    expand_replies: bool = True,
    max_replies: int = 10,
    progress: bool = True,
    first_thread_timeout: float = 30.0,
) -> list[dict]:
    """Load up to ``limit`` top-level comments and harvest them with replies.

    Returns an ordered list of records, each comment immediately followed by its
    replies::

        {"kind": "comment"|"reply", "author": str, "html": str,
         "likes": str, "date_raw": str, "parent_author": str}  # parent on replies
    """
    # Bring the comments section into view to trigger the first fetch.
    driver.execute_script("window.scrollTo(0, 800);")
    driver.execute_script(
        "var c=document.querySelector(arguments[0]); if (c) { c.scrollIntoView(); }",
        COMMENTS_CONTAINER,
    )
    # Wait for the first thread, actively re-nudging the lazy-load observer each
    # slice; bail fast (and cleanly) when comments are turned off for the video.
    deadline = time.time() + first_thread_timeout
    while time.time() < deadline:
        if _comments_disabled(driver):
            log.warning("comments are turned off for this video")
            return []
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, COMMENT_THREAD))
            )
            break
        except TimeoutException:
            driver.execute_script("window.scrollBy(0, 1200);")
            driver.execute_script(
                "var c=document.querySelector(arguments[0]); if (c) c.scrollIntoView();",
                COMMENTS_CONTAINER,
            )
    else:
        log.warning("no comment threads appeared — comments disabled, empty, or blocked")
        return []

    # ---- Phase A: load top-level comments -------------------------------- #
    log.info("loading up to %d top-level comments (scroll budget %dx)", limit, max_viewports)
    stale = 0
    with tqdm(
        total=limit, desc="loading comments", unit="cmt", leave=False, disable=not progress
    ) as bar:
        for _ in range(max_viewports):
            before = len(driver.find_elements(By.CSS_SELECTOR, COMMENT_THREAD))
            bar.n = min(before, limit)
            bar.refresh()
            if before >= limit:
                break
            _scroll_for_more(driver)
            try:
                WebDriverWait(driver, 10).until(
                    lambda d, before=before: (
                        len(d.find_elements(By.CSS_SELECTOR, COMMENT_THREAD)) > before
                    )
                )
                stale = 0
            except TimeoutException:
                # Re-arm the observer with an up-nudge, then retry once before
                # counting this as a no-growth (stall) iteration.
                driver.execute_script("window.scrollBy(0, -600);")
                time.sleep(0.6)
                _scroll_for_more(driver)
                try:
                    WebDriverWait(driver, 8).until(
                        lambda d, before=before: (
                            len(d.find_elements(By.CSS_SELECTOR, COMMENT_THREAD)) > before
                        )
                    )
                    stale = 0
                except TimeoutException:
                    stale += 1
                    if stale >= 3:  # 3 consecutive no-growth scrolls => end of list
                        break
            time.sleep(0.4)
        loaded_now = len(driver.find_elements(By.CSS_SELECTOR, COMMENT_THREAD))
        bar.n = min(loaded_now, limit)
        bar.refresh()

    threads = driver.find_elements(By.CSS_SELECTOR, COMMENT_THREAD)[:limit]
    log.info(
        "loaded %d top-level comments; harvesting%s",
        len(threads),
        " + replies" if expand_replies and max_replies > 0 else "",
    )

    # ---- Phase B: expand + harvest, each thread exactly once ------------- #
    records: list[dict] = []
    desc = "harvesting (+replies)" if expand_replies and max_replies > 0 else "harvesting"
    for th in tqdm(threads, desc=desc, unit="thread", leave=False, disable=not progress):
        # A single thread going stale mid-harvest must not abort the whole run;
        # the inner field reads each swallow their own errors, this catches the
        # rarer case of `th`/`top` itself going stale between reads.
        try:
            top = _top_el(th)
            _scroll_into_view(driver, top)
            _click_read_more(driver, top)

            html = _first_inner_html(top, COMMENT_TEXT_SELECTORS)
            if not html.strip():
                continue  # truly empty comment body: skip it (and its replies)
            parent_author = _first_text(top, COMMENT_AUTHOR_SELECTORS)
            records.append(
                {
                    "kind": "comment",
                    "author": parent_author,
                    "html": html,
                    "likes": _likes(top),
                    "date_raw": _first_text(top, PUBLISHED_TIME_SELECTORS),
                }
            )

            if expand_replies and max_replies > 0:
                _expand_replies(driver, th, max_replies)
                try:
                    reply_els = th.find_elements(By.CSS_SELECTOR, REPLY_ITEM)[:max_replies]
                    if not reply_els:  # primary selector found nothing — try the fallback
                        reply_els = th.find_elements(By.CSS_SELECTOR, REPLY_ITEM_FALLBACK)[
                            :max_replies
                        ]
                except (StaleElementReferenceException, WebDriverException):
                    reply_els = []
                for rep in reply_els:
                    r_html = _first_inner_html(rep, COMMENT_TEXT_SELECTORS)
                    if not r_html.strip():
                        continue
                    records.append(
                        {
                            "kind": "reply",
                            "author": _first_text(rep, COMMENT_AUTHOR_SELECTORS),
                            "parent_author": parent_author,
                            "html": r_html,
                            "likes": _likes(rep),
                            "date_raw": _first_text(rep, PUBLISHED_TIME_SELECTORS),
                        }
                    )
        except (StaleElementReferenceException, WebDriverException):
            continue

    n_top = sum(1 for r in records if r["kind"] == "comment")
    log.info(
        "collected %d records (%d comments + %d replies)", len(records), n_top, len(records) - n_top
    )
    return records


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #
def _js_click(driver, el) -> bool:
    """Scroll into view and dispatch a JS ``.click()``.

    For the transcript controls a JS click is what actually opens the engagement
    panel (verified by probe: it loads all segments), whereas a trusted Selenium
    ``.click()`` can no-op here — the opposite of the reply toggles, so we do NOT
    use ``_safe_click`` for the transcript flow."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        driver.execute_script("arguments[0].click();", el)
        return True
    except (StaleElementReferenceException, WebDriverException):
        return False


def _expand_description(driver) -> None:
    """Click the description "...more" expander if present and visible.

    Idempotent: when the description is already expanded the #expand button is
    hidden, so ``_displayed`` is False and we skip it (avoids collapsing it). The
    transcript section lives inside the expanded description.
    """
    try:
        for e in driver.find_elements(By.CSS_SELECTOR, DESC_EXPAND):
            if _displayed(e):
                _js_click(driver, e)
                time.sleep(0.4)
                break
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass


def _find_transcript_button(driver):
    """Return the "Show transcript" button element, or None.

    Prefers the description transcript section; falls back to any visible button
    whose aria-label/text matches /transcri/ — excluding a "Hide/Ocultar" toggle
    (the panel may linger from a previous video on a reused driver)."""
    try:
        for sec in driver.find_elements(By.CSS_SELECTOR, TRANSCRIPT_SECTION):
            for b in sec.find_elements(By.CSS_SELECTOR, "button"):
                if _displayed(b):
                    return b
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass
    try:
        return driver.execute_script(
            "var bs=document.querySelectorAll('button');"
            "for (var i=0;i<bs.length;i++){var b=bs[i];"
            "var s=((b.getAttribute('aria-label')||'')+' '+(b.textContent||'')).toLowerCase();"
            "if(/transcri/.test(s) && !/hide|ocultar/.test(s)) return b;}"
            "return null;"
        )
    except WebDriverException:
        return None


def _load_all_transcript_segments(driver, max_rounds: int = 60) -> int:
    """Defensive against virtualization: scroll the last segment into view until
    the segment count stops growing. Returns the final count. (For the common
    non-virtualized panel this stabilises immediately.)"""
    prev, stale = -1, 0
    for _ in range(max_rounds):
        try:
            n = len(driver.find_elements(By.CSS_SELECTOR, TRANSCRIPT_SEGMENT))
        except WebDriverException:
            break
        if n == prev:
            stale += 1
            if stale >= 2:
                break
        else:
            stale = 0
        prev = n
        try:
            driver.execute_script(
                "var s=document.querySelectorAll(arguments[0]);"
                "if (s.length) s[s.length - 1].scrollIntoView({block: 'end'});",
                TRANSCRIPT_SEGMENT,
            )
        except WebDriverException:
            break
        time.sleep(0.3)
    return prev if prev > 0 else 0


def fetch_transcript(driver, progress: bool = True, timeout: float = 12.0) -> list[tuple[str, str]]:
    """Open the transcript panel and return ``[(timestamp, text), ...]``.

    Returns ``[]`` when the video has no transcript (no button, or the panel
    opens empty — common for music videos) or on ANY error: this NEVER raises, so
    a transcript failure can't discard already-harvested comments or recycle the
    pooled driver. Reads via ``textContent`` (off-screen segments would be ""
    under Selenium ``.text``) and extracts all segments in a single round-trip.
    """
    try:
        if progress:
            log.info("fetching transcript…")
        driver.execute_script("window.scrollTo(0, 600);")
        _expand_description(driver)

        btn = _find_transcript_button(driver)
        if btn is None:
            log.info("no transcript button — transcript unavailable for this video")
            return []
        if not _js_click(driver, btn):
            log.warning("found the transcript button but could not click it")
            return []

        try:
            WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, TRANSCRIPT_SEGMENT))
            )
        except TimeoutException:
            log.info("transcript panel opened but no segments — transcript unavailable")
            return []

        _load_all_transcript_segments(driver)

        raw = (
            driver.execute_script(
                "var out=[];var segs=document.querySelectorAll(arguments[0]);"
                "for (var i=0;i<segs.length;i++){var s=segs[i];"
                "var t=s.querySelector(arguments[1]);"
                "var x=s.querySelector(arguments[2]);"
                "out.push([t?t.textContent:'', x?x.textContent:'']);}"
                "return out;",
                TRANSCRIPT_SEGMENT,
                TRANSCRIPT_TS,
                TRANSCRIPT_SEG_TEXT,
            )
            or []
        )

        segments: list[tuple[str, str]] = []
        for ts, txt in raw:
            txt = " ".join((txt or "").split())
            if not txt:  # only drop truly empty text; "[Music]"/"♪" are real content
                continue
            segments.append((" ".join((ts or "").split()), txt))
        log.info("transcript: %d segments", len(segments))
        return segments
    except (
        TimeoutException,
        StaleElementReferenceException,
        NoSuchElementException,
        WebDriverException,
    ) as exc:
        log.warning("transcript fetch failed: %s", exc)
        return []
    except Exception as exc:  # pragma: no cover - never break the video over a transcript
        log.warning("transcript fetch failed unexpectedly: %s", exc)
        return []
