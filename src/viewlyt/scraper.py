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
import random
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
# Union of both reply selectors, used to WAIT for and COUNT replies during expansion.
# The primary REPLY_ITEM often misses current YouTube reply DOM (replies render
# outside ytd-comment-replies-renderer/#replies), so waiting on it alone TIMES OUT
# ~6s/thread even though the reply loaded — and the continuation loop, counting via
# the same miss, never fired, leaving most replies uncollected. The union resolves
# as soon as a reply appears and counts every reply, fixing both the stall and the
# under-collection.
REPLY_ITEM_ANY = f"{REPLY_ITEM}, {REPLY_ITEM_FALLBACK}"
REPLY_CONTINUATION = "ytd-comment-replies-renderer ytd-continuation-item-renderer"

# Localized "comments are turned off" markers — lets the load phase bail in <1s
# instead of waiting out the whole first-thread timeout on a disabled section.
COMMENTS_OFF_MARKERS = (
    "comments are turned off",
    "comentários estão desativados",
    "comentários foram desativados",
    "los comentarios están desactivados",
)

# Related videos (watch-page secondary column). YouTube retired the legacy
# ytd-compact-video-renderer in favour of <yt-lockup-view-model>; Shorts use a
# DIFFERENT tag (ytm-shorts-lockup-view-model), so selecting the lockup tag skips
# them for free. The sidebar exposes title + url + views (NO likes — likes live
# only on each video's own page).
RELATED_ITEM_SELECTORS = (
    "#secondary yt-lockup-view-model",
    "ytd-watch-next-secondary-results-renderer yt-lockup-view-model",
)
# Title node inside a lockup — ordered fallback, EXACT class tokens (never a
# substring class matcher; see test_transcript_timestamp_exact_token).
RELATED_TITLE_SELECTORS = (
    ".yt-lockup-metadata-view-model-wiz__title",
    "h3 a",
    "a[title]",
    "span[role=text]",
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
# Exact-token class selectors (CSS `.token` is exact-token by spec, so it can never
# match the sibling ...TimestampA11yLabel). NEVER weaken these to substring matches.
TRANSCRIPT_TS = ".ytwTranscriptSegmentViewModelTimestamp, .segment-timestamp"
TRANSCRIPT_SEG_TEXT = "span.ytAttributedStringHost, yt-formatted-string.segment-text, .segment-text"
# Broader panel-host match (modern + legacy + bare renderers) to confirm the panel
# opened even when the click target was a menu item or a direct engagement-panel open.
TRANSCRIPT_PANEL_ANY = (
    'ytd-engagement-panel-section-list-renderer[target-id="engagement-panel-searchable-transcript"], '
    "ytd-transcript-renderer, ytd-transcript-search-panel-renderer"
)
# Scrollable host of the (often virtualized) segment list — scrolling THIS, not the
# last segment node, is what keeps a virtualized list yielding new rows.
TRANSCRIPT_SCROLLER = (
    "ytd-transcript-segment-list-renderer #segments-container, "
    "#segments-container, "
    "ytd-engagement-panel-section-list-renderer #content"
)
# The "...more"/overflow action menu that, on some layouts, hosts "Show transcript".
OVERFLOW_MENU_BUTTON = (
    'ytd-watch-metadata button[aria-label="More actions"], '
    'button[aria-label="More actions"], button[aria-label="Mais ações"]'
)

# Locale-dependent "before you continue" consent button labels (best-effort
# fallback; the cookie priming below usually skips the interstitial entirely).
# Matched case-insensitively against aria-label + textContent of button/a nodes.
CONSENT_LABELS = ("accept all", "aceitar tudo", "reject all", "rejeitar tudo")

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


# Single-round-trip scan for a VISIBLE consent button (label match on aria-label
# + textContent). One cheap RPC instead of a WebDriverWait that burns its whole
# timeout on the (usual, cookie-primed) no-dialog case.
_CONSENT_SCAN_JS = r"""
var LABELS=arguments[0];
var els=document.querySelectorAll('button, a');
for(var i=0;i<els.length;i++){var el=els[i];
  var s=((el.getAttribute('aria-label')||'')+' '+(el.textContent||'')).toLowerCase();
  for(var j=0;j<LABELS.length;j++){
    if(s.indexOf(LABELS[j])!==-1 && el.offsetParent!==null){return el;}}}
return null;
"""


def dismiss_consent_dialog(driver, timeout: float = 4.0) -> bool:
    """Best-effort click of a cookie-consent button if one is shown.

    Fast path: with consent cookies primed the dialog almost never exists, so
    this scans once (plus one 0.3s-later rescan for a late-rendering dialog) and
    returns immediately instead of waiting out ``timeout`` on every video —
    that dead wait cost ~2s/video. A real consent WALL (not just a dialog) is
    still caught later by :func:`detect_block`. ``timeout`` caps the rescan.
    """
    try:
        btn = driver.execute_script(_CONSENT_SCAN_JS, list(CONSENT_LABELS))
        if btn is None and timeout > 0:
            time.sleep(min(0.3, timeout))
            btn = driver.execute_script(_CONSENT_SCAN_JS, list(CONSENT_LABELS))
        if btn is None:
            return False
        driver.execute_script("arguments[0].click();", btn)
        log.info("dismissed consent dialog")
        time.sleep(0.8)
        return True
    except WebDriverException:
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
                "var c=document.querySelector('ytd-comments,#comments');"
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
# Waits in the hot paths poll at this frequency instead of Selenium's 0.5s
# default — the condition usually flips within tens of ms of the DOM update, so
# the default poll wastes ~0.35s per satisfied wait, many times per video.
_POLL = 0.15


def _count(driver, css: str) -> int:
    """Number of ``css`` matches in the document, via ONE light ``execute_script``
    round-trip returning an int. ``find_elements`` would materialise (and ship
    back) N element handles just to be counted — and the growth waits call this
    on every poll."""
    return int(
        driver.execute_script("return document.querySelectorAll(arguments[0]).length", css) or 0
    )


def _count_in(driver, el, css: str) -> int:
    """Like :func:`_count`, scoped to descendants of ``el``."""
    return int(
        driver.execute_script("return arguments[0].querySelectorAll(arguments[1]).length", el, css)
        or 0
    )


def _sleep_jitter(base: float, spread: float = 0.4) -> None:
    """Sleep ``base`` ± ``spread``·``base`` (uniform). Fixed-interval scrolling is
    both slower than needed on fast pages and a metronome-like automation tell;
    the jitter keeps the MEAN at ``base`` while breaking the rhythm."""
    time.sleep(max(0.05, base * random.uniform(1 - spread, 1 + spread)))


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
        WebDriverWait(driver, 6, poll_frequency=_POLL).until(
            lambda d: _count_in(d, thread, REPLY_ITEM_ANY) > 0
        )
    except (TimeoutException, StaleElementReferenceException, WebDriverException):
        return

    for _ in range(max_more_clicks):
        try:
            before = _count_in(driver, thread, REPLY_ITEM_ANY)
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
            WebDriverWait(driver, 5, poll_frequency=_POLL).until(
                lambda d, before=before: _count_in(d, thread, REPLY_ITEM_ANY) > before
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
# Single-round-trip harvest of one thread
# --------------------------------------------------------------------------- #
# Reads the top comment + every reply (author / html / likes / date) for ONE thread
# in a single in-page pass, mirroring _first_text/_first_inner_html/_likes/_top_el:
# ordered selector fallbacks, whitespace-collapsed textContent, raw innerHTML, and a
# '0' likes default. This replaces ~13 + 8·(replies) Selenium round-trips per thread
# with one. The top comment's "Read more" is clicked only when visible (offsetParent)
# so an already-expanded comment is never re-collapsed. Returns null for a genuinely
# empty top comment (the caller skips it); reply slicing matches the Python path
# (first `max_replies` reply elements, then drop the empty ones). The JS `/\s+/`
# collapse matches Python's str.split() for all realistic author/date/likes text
# (they differ only on rare control separators like U+001C-1F, never seen here);
# `html` is raw innerHTML in both paths, so the comment body is byte-identical.
_HARVEST_JS = r"""
var th=arguments[0], TOPS=arguments[1], TXT=arguments[2], AUTH=arguments[3],
    LIKES=arguments[4], TIME=arguments[5], REPLY=arguments[6], REPLY_FB=arguments[7],
    MORE=arguments[8], MAXR=arguments[9];
function ft(el,sels){for(var i=0;i<sels.length;i++){var n=el.querySelector(sels[i]);
  if(n){var t=(n.textContent||'').replace(/\s+/g,' ').trim();if(t)return t;}}return '';}
function fh(el,sels){for(var i=0;i<sels.length;i++){var n=el.querySelector(sels[i]);
  if(n){var h=n.innerHTML||'';if(h.trim())return h;}}return '';}
function topEl(th){for(var i=0;i<TOPS.length;i++){var n=th.querySelector(TOPS[i]);if(n)return n;}return th;}
var t=topEl(th);
try{var mb=t.querySelector(MORE);if(mb&&mb.offsetParent!==null)mb.click();}catch(e){}
var html=fh(t,TXT);
if(!html.trim())return null;
var rec={author:ft(t,AUTH),html:html,likes:(ft(t,LIKES)||'0'),date:ft(t,TIME),replies:[]};
if(MAXR>0){
  var reps=th.querySelectorAll(REPLY);
  if(!reps.length)reps=th.querySelectorAll(REPLY_FB);
  var lim=Math.min(reps.length,MAXR);
  for(var i=0;i<lim;i++){var rp=reps[i];var rh=fh(rp,TXT);
    if(!rh.trim())continue;
    rec.replies.push({author:ft(rp,AUTH),html:rh,likes:(ft(rp,LIKES)||'0'),date:ft(rp,TIME)});}
}
return rec;
"""


def _harvest_thread(driver, th, max_replies: int):
    """Harvest one thread in a single ``execute_script`` round-trip.

    Returns the comment record (with a ``replies`` list of records) or ``None`` for an
    empty top comment. Lets ``WebDriverException`` propagate so the caller can fall
    back to the per-element path."""
    return driver.execute_script(
        _HARVEST_JS,
        th,
        list(TOP_COMMENT_SELECTORS),
        list(COMMENT_TEXT_SELECTORS),
        list(COMMENT_AUTHOR_SELECTORS),
        list(LIKES_SELECTORS),
        list(PUBLISHED_TIME_SELECTORS),
        REPLY_ITEM,
        REPLY_ITEM_FALLBACK,
        READ_MORE,
        max_replies,
    )


def _harvest_thread_fallback(driver, th, max_replies: int):
    """Per-element harvest (the proven, chatty path) — used only when the batched JS
    read errors. Returns the same record shape as :func:`_harvest_thread`."""
    top = _top_el(th)
    _scroll_into_view(driver, top)
    _click_read_more(driver, top)
    html = _first_inner_html(top, COMMENT_TEXT_SELECTORS)
    if not html.strip():
        return None
    rec = {
        "author": _first_text(top, COMMENT_AUTHOR_SELECTORS),
        "html": html,
        "likes": _likes(top),
        "date": _first_text(top, PUBLISHED_TIME_SELECTORS),
        "replies": [],
    }
    if max_replies > 0:
        try:
            reply_els = th.find_elements(By.CSS_SELECTOR, REPLY_ITEM)[:max_replies]
            if not reply_els:
                reply_els = th.find_elements(By.CSS_SELECTOR, REPLY_ITEM_FALLBACK)[:max_replies]
        except (StaleElementReferenceException, WebDriverException):
            reply_els = []
        for rep in reply_els:
            r_html = _first_inner_html(rep, COMMENT_TEXT_SELECTORS)
            if not r_html.strip():
                continue
            rec["replies"].append(
                {
                    "author": _first_text(rep, COMMENT_AUTHOR_SELECTORS),
                    "html": r_html,
                    "likes": _likes(rep),
                    "date": _first_text(rep, PUBLISHED_TIME_SELECTORS),
                }
            )
    return rec


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
            WebDriverWait(driver, 5, poll_frequency=_POLL).until(
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
            before = _count(driver, COMMENT_THREAD)
            bar.n = min(before, limit)
            bar.refresh()
            if before >= limit:
                break
            _scroll_for_more(driver)
            try:
                WebDriverWait(driver, 10, poll_frequency=_POLL).until(
                    lambda d, before=before: _count(d, COMMENT_THREAD) > before
                )
                stale = 0
            except TimeoutException:
                # Re-arm the observer with an up-nudge, then retry once before
                # counting this as a no-growth (stall) iteration.
                driver.execute_script("window.scrollBy(0, -600);")
                time.sleep(0.6)
                _scroll_for_more(driver)
                try:
                    WebDriverWait(driver, 8, poll_frequency=_POLL).until(
                        lambda d, before=before: _count(d, COMMENT_THREAD) > before
                    )
                    stale = 0
                except TimeoutException:
                    stale += 1
                    if stale >= 3:  # 3 consecutive no-growth scrolls => end of list
                        break
            _sleep_jitter(0.35)  # render settle; jittered (see _sleep_jitter)
        loaded_now = _count(driver, COMMENT_THREAD)
        bar.n = min(loaded_now, limit)
        bar.refresh()

    threads = driver.find_elements(By.CSS_SELECTOR, COMMENT_THREAD)[:limit]
    log.info(
        "loaded %d top-level comments; harvesting%s",
        len(threads),
        " + replies" if expand_replies and max_replies > 0 else "",
    )

    # ---- Phase B: expand replies (trusted clicks), then harvest each thread in a
    # single in-page round-trip — falling back to the per-element path on any JS
    # error. Interleaved per thread (expand -> read) so a long, virtualizing list
    # can't recycle an early thread's replies before we read them. --------------- #
    records: list[dict] = []
    eff_max_replies = max_replies if expand_replies else 0
    desc = "harvesting (+replies)" if eff_max_replies > 0 else "harvesting"
    for th in tqdm(threads, desc=desc, unit="thread", leave=False, disable=not progress):
        # A single thread going stale mid-harvest must not abort the whole run.
        try:
            if eff_max_replies > 0:
                _expand_replies(driver, th, eff_max_replies)
            try:
                rec = _harvest_thread(driver, th, eff_max_replies)
            except (StaleElementReferenceException, WebDriverException):
                rec = _harvest_thread_fallback(driver, th, eff_max_replies)
            if not rec:
                continue  # truly empty comment body: skip it (and its replies)
            parent_author = rec.get("author") or ""
            records.append(
                {
                    "kind": "comment",
                    "author": parent_author,
                    "html": rec.get("html") or "",
                    "likes": rec.get("likes") or "0",
                    "date_raw": rec.get("date") or "",
                }
            )
            for rp in rec.get("replies") or []:
                records.append(
                    {
                        "kind": "reply",
                        "author": rp.get("author") or "",
                        "parent_author": parent_author,
                        "html": rp.get("html") or "",
                        "likes": rp.get("likes") or "0",
                        "date_raw": rp.get("date") or "",
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
# Related videos (secondary column)
# --------------------------------------------------------------------------- #
# Single-round-trip read of the secondary column: for each <yt-lockup-view-model>
# take the first /watch?v= link's id, the title (ordered selector fallback, then
# the link's title attr), and the views metadata span (the one whose text matches
# view/visualiz — the sibling spans are the channel, a "•", and the upload date).
# De-dups by id and canonicalises the url. Mirrors _HARVEST_JS's ft()/fallback style.
_RELATED_JS = r"""
var ITEM=arguments[0], TITLE=arguments[1], LIMIT=arguments[2];
var items=document.querySelectorAll(ITEM), out=[], seen={};
for(var i=0;i<items.length && out.length<LIMIT;i++){
  var lu=items[i];
  var a=lu.querySelector("a[href*='/watch?v=']");
  if(!a)continue;
  var m=(a.getAttribute('href')||'').match(/[?&]v=([A-Za-z0-9_-]{11})/);
  if(!m||seen[m[1]])continue;
  var id=m[1], title='';
  for(var k=0;k<TITLE.length;k++){var tn=lu.querySelector(TITLE[k]);
    if(tn){var tt=(tn.textContent||'').replace(/\s+/g,' ').trim();if(tt){title=tt;break;}}}
  if(!title)title=(a.getAttribute('title')||'').replace(/\s+/g,' ').trim();
  if(!title)continue;
  var views='', meta=lu.querySelector('yt-content-metadata-view-model');
  if(meta){var sp=meta.querySelectorAll('span');
    for(var j=0;j<sp.length;j++){var vx=(sp[j].textContent||'').replace(/\s+/g,' ').trim();
      if(/view|visualiz/i.test(vx)){views=vx;break;}}}
  seen[id]=1;
  out.push({video_id:id,title:title,views:views,url:'https://www.youtube.com/watch?v='+id});
}
return out;
"""


def collect_related(driver, limit: int = 10, progress: bool = True) -> list[dict]:
    """Collect up to ``limit`` related videos from the watch page's secondary column.

    Returns a list of ``{"video_id", "title", "views", "url"}`` dicts. ``views`` is
    YouTube's own sidebar text (e.g. "1.2B views") — the sidebar exposes NO likes,
    only views. NEVER raises (returns ``[]`` on any error), like
    :func:`fetch_transcript`, so a related-collection hiccup can't discard
    already-harvested comments or recycle the pooled driver.
    """
    if limit <= 0:
        return []
    sel = ", ".join(RELATED_ITEM_SELECTORS)
    try:
        if progress:
            log.info("collecting up to %d related videos", limit)
        # The secondary column hydrates lazily; nudge-scroll until enough lockups
        # are present or the count stops growing (stale guard handles short lists).
        driver.execute_script("window.scrollTo(0, 600);")
        prev, stale = -1, 0
        for _ in range(12):
            n = _count(driver, sel)
            if n >= limit:
                break
            if n == prev:
                stale += 1
                if stale >= 3:
                    break
            else:
                stale = 0
            prev = n
            driver.execute_script("window.scrollBy(0, 1200);")
            _sleep_jitter(0.35)
        items = driver.execute_script(_RELATED_JS, sel, list(RELATED_TITLE_SELECTORS), limit) or []
        if progress:
            log.info("collected %d related videos", len(items))
        return items[:limit]
    except WebDriverException as exc:
        log.warning("related collection failed: %s", exc)
        return []


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
                time.sleep(0.25)
                break
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass


def _scan_transcript_control(driver):
    """JS scan for a VISIBLE control whose aria-label/text matches /transcri/,
    excluding a "Hide/Ocultar" toggle (the panel may linger from a previous video
    on a reused driver). Covers buttons, links, and menu items; ``offsetParent``
    rules out hidden/lingering controls. Returns the element or None."""
    try:
        return driver.execute_script(
            "var bs=document.querySelectorAll('button, a, yt-button-shape, "
            "ytd-button-renderer, tp-yt-paper-button, ytd-menu-service-item-renderer, "
            "tp-yt-paper-item');"
            "for (var i=0;i<bs.length;i++){var b=bs[i];"
            "var s=((b.getAttribute('aria-label')||'')+' '+(b.textContent||'')).toLowerCase();"
            "if(/transcri/.test(s) && !/hide|ocultar/.test(s) && b.offsetParent!==null) return b;}"
            "return null;"
        )
    except WebDriverException:
        return None


def _find_transcript_button(driver):
    """Return the "Show transcript" button element, or None.

    Tries, in order: (1) the description transcript section button; (2) any visible
    /transcri/ control anywhere; (3) the "...more" action overflow menu, which on
    some layouts hosts the entry — opened, then rescanned."""
    try:
        for sec in driver.find_elements(By.CSS_SELECTOR, TRANSCRIPT_SECTION):
            for b in sec.find_elements(By.CSS_SELECTOR, "button"):
                if _displayed(b):
                    return b
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass

    btn = _scan_transcript_control(driver)
    if btn is not None:
        return btn

    # Open the "...more" overflow menu (cheap) and rescan — the transcript entry
    # lives inside it on some layouts.
    try:
        for m in driver.find_elements(By.CSS_SELECTOR, OVERFLOW_MENU_BUTTON):
            if _displayed(m):
                _js_click(driver, m)
                time.sleep(0.25)
                break
    except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
        pass
    return _scan_transcript_control(driver)


def _open_transcript_panel_direct(driver) -> bool:
    """Best-effort: ask the page to open the searchable-transcript engagement panel
    directly, for videos where no clickable "Show transcript" control is reachable.

    Returns whether the attempt was dispatched (never raises); the caller still
    verifies via the panel/segment wait, so a no-op simply degrades to []."""
    try:
        driver.execute_script(
            "try{var app=document.querySelector('ytd-app');"
            "if(app&&app.fire){app.fire('yt-action',{actionName:"
            "'yt-open-engagement-panel-action',args:[{openEngagementPanelAction:"
            "{panelIdentifier:'engagement-panel-searchable-transcript'}}],"
            "optionalAction:false,returnValue:[]});}}catch(e){}"
        )
        return True
    except WebDriverException:
        return False


def _wait_for_segments(driver, timeout: float) -> int:
    """Wait for at least one transcript segment to appear; return the count (0 on
    timeout / error). Never raises."""
    try:
        WebDriverWait(driver, timeout, poll_frequency=_POLL).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, TRANSCRIPT_SEGMENT))
        )
        return _count(driver, TRANSCRIPT_SEGMENT)
    except (TimeoutException, StaleElementReferenceException, WebDriverException):
        return 0


# In-page scroll-until-stable loop for the (possibly virtualized) segment list:
# count → (2 stable reads in a row, or round budget spent) → done(count); else
# scroll the panel container (that's what advances virtualization; fall back to
# the last segment) and tick again in 150ms. ONE execute_async_script round-trip
# replaces 2 RPCs + a 0.3s Python sleep PER ROUND (the common non-virtualized
# panel stabilises in ~2 ticks; a long virtualized list, in dozens).
_LOAD_SEGMENTS_JS = r"""
var SCROLLER=arguments[0], SEG=arguments[1], MAXR=arguments[2], done=arguments[3];
var prev=-1, stale=0, rounds=0;
function tick(){
  var n=document.querySelectorAll(SEG).length;
  if(n===prev){stale++;}else{stale=0;prev=n;}
  if(stale>=2||rounds>=MAXR){done(prev>0?prev:0);return;}
  rounds++;
  var sc=document.querySelector(SCROLLER);
  if(sc&&sc.scrollHeight>sc.clientHeight){sc.scrollTop=sc.scrollHeight;}
  else{var s=document.querySelectorAll(SEG);if(s.length){s[s.length-1].scrollIntoView({block:'end'});}}
  setTimeout(tick,150);
}
tick();
"""


def _load_all_transcript_segments(driver, max_rounds: int = 60) -> int:
    """Load every (possibly virtualized) transcript segment; return the count.

    Fast path: the whole scroll-until-stable loop runs in-page via ONE
    ``execute_async_script`` (see ``_LOAD_SEGMENTS_JS``). Falls back to the
    per-round Selenium loop if async scripts are unavailable/error."""
    try:
        return int(
            driver.execute_async_script(
                _LOAD_SEGMENTS_JS, TRANSCRIPT_SCROLLER, TRANSCRIPT_SEGMENT, max_rounds
            )
            or 0
        )
    except WebDriverException:
        return _load_all_transcript_segments_slow(driver, max_rounds)


def _load_all_transcript_segments_slow(driver, max_rounds: int = 60) -> int:
    """Per-round Selenium fallback for :func:`_load_all_transcript_segments`."""
    prev, stale = -1, 0
    for _ in range(max_rounds):
        try:
            n = _count(driver, TRANSCRIPT_SEGMENT)
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
                # Prefer scrolling the (virtualized) panel container — that is what
                # advances virtualization; fall back to bringing the last segment
                # into view for the legacy / non-virtualized panel.
                "var sc=document.querySelector(arguments[0]);"
                "if(sc && sc.scrollHeight>sc.clientHeight){sc.scrollTop=sc.scrollHeight;return;}"
                "var s=document.querySelectorAll(arguments[1]);"
                "if(s.length) s[s.length-1].scrollIntoView({block:'end'});",
                TRANSCRIPT_SCROLLER,
                TRANSCRIPT_SEGMENT,
            )
        except WebDriverException:
            break
        time.sleep(0.2)
    return prev if prev > 0 else 0


def _open_transcript_and_wait(
    driver, timeout: float, button_timeout: float = 0.0
) -> tuple[int, bool]:
    """Open the transcript panel and wait for segments to hydrate.

    Returns ``(segment_count, had_button)``. ``had_button`` tells "the video
    advertises a transcript" (a real control was found) apart from "no transcript
    at all", so the caller can decide whether an empty panel is worth a recovery
    attempt. May raise WebDriverException — the caller handles it.

    ``button_timeout`` polls for the transcript control instead of the default
    instant scan. Needed right after a page (re)load: with the *eager* page-load
    strategy the description section often hasn't rendered yet, so an instant
    scan misses a button that appears 1–3s later and the flow silently degrades
    to the short direct-open budget. On a long-settled page keep it at 0.

    A real button gets the full timeout and one retry (a reused driver's first
    click can no-op while a prior panel tears down); the speculative direct-open
    (no button at all — usually a genuinely transcript-less video) gets a short
    budget so music videos stay fast.
    """
    driver.execute_script("window.scrollTo(0, 600);")
    _expand_description(driver)

    btn = _find_transcript_button(driver)
    if btn is None and button_timeout > 0:
        deadline = time.time() + button_timeout
        while btn is None and time.time() < deadline:
            time.sleep(0.5)
            _expand_description(driver)  # idempotent; the section lives inside it
            btn = _find_transcript_button(driver)
    if btn is not None:
        if not _js_click(driver, btn):
            log.warning("found the transcript button but could not click it")
            return 0, True
        seg_timeout, retry = timeout, True
        # Panel host usually appears before the segments hydrate; don't block long.
        try:
            WebDriverWait(driver, min(seg_timeout, 4.0), poll_frequency=_POLL).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, TRANSCRIPT_PANEL_ANY))
            )
        except TimeoutException:
            pass
    else:
        if not _open_transcript_panel_direct(driver):
            log.info("no transcript control — transcript unavailable for this video")
            return 0, False
        seg_timeout, retry = min(timeout, 1.2), False

    n_seg = _wait_for_segments(driver, seg_timeout)
    if n_seg == 0 and retry:
        _js_click(driver, btn)
        n_seg = _wait_for_segments(driver, seg_timeout)
    return n_seg, btn is not None


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
        n_seg, had_button = _open_transcript_and_wait(driver, timeout)
        if n_seg == 0 and had_button:
            # Dirty-page failure mode (probe-verified): after heavy comment
            # scrolling and/or sidebar collection, the panel opens EXPANDED but
            # its transcript data request hangs forever — spinner stuck, zero
            # segments — and the in-page retry click is unreliable (it recovered
            # a light page, not a heavy one). Reloading the same watch URL is
            # deterministic: on a fresh page the panel hydrates in ~1s. Only
            # worth it when a real button existed (the video advertises a
            # transcript) — keeps genuinely transcript-less videos fast.
            log.info("transcript panel stuck empty — reloading the page and retrying once")
            safe_get(driver, driver.current_url)
            # The reloaded page is cold (eager load strategy): poll for the
            # button instead of the instant scan, or the retry silently falls
            # into the short direct-open budget and misses the transcript.
            n_seg, had_button = _open_transcript_and_wait(driver, timeout, button_timeout=10.0)
        if n_seg == 0:
            if had_button:
                log.info("transcript panel opened but no segments — transcript unavailable")
            return []

        _load_all_transcript_segments(driver)

        raw = (
            driver.execute_script(
                "var out=[];var segs=document.querySelectorAll(arguments[0]);"
                "for (var i=0;i<segs.length;i++){var s=segs[i];"
                "var t=s.querySelector(arguments[1]);"
                "var x=s.querySelector(arguments[2]);"
                "var tt=t?t.textContent:'';"
                # Text fallback: when neither text host matches, use the segment's
                # own text minus the timestamp so a layout change still yields text.
                "var xt=x?x.textContent:(s.textContent||'').replace(tt,'');"
                "out.push([tt, xt]);}"
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
