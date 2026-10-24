"""Browser e2e for the live dashboard UI on WEBKIT (Safari's engine).

Complements ``test_live_e2e.py`` (the connect/ingest/export flow). This module
drives the *dashboard surface*: theme, settings drawer, probe lifecycle, all 12
chart types, decider status, the chat feed, the phone layout, the two-step grid
(2D) remove, reduced motion and an accessibility smoke.

What boots, per module:

* a FAKE OpenAI-compatible chat endpoint (tiny FastAPI app on a free port) that
  answers ``{"labels": [...]}`` for classification schemas (one label per
  numbered line, cycling through the schema's enum) and ``{"summary": "..."}``
  for open ones, with ``usage.cost`` -- so analyses finish deterministically and
  nothing leaves the machine;
* the real FastAPI app (``create_app`` + uvicorn in a thread, bound to
  ``127.0.0.1`` and addressed as ``127.0.0.1`` because the server pins ``Host``),
  UNPAUSED, with ``LLMConfig(base_url=<fake>)``;
* persistence redirected to ``tmp_path`` exactly like ``test_live_e2e.py`` (the
  real ``~/.viewlyt`` is never read or written).

Most scenarios get their own fresh server (``fresh_live``) so state never leaks
between tests; the read-mostly ones share one pre-seeded server (``live``).
Every test opens a brand-new browser context (empty localStorage), collects
console errors / uncaught page errors and asserts there were none.

Mutation testing hook: ``live_server.STATIC_DIR`` is monkeypatched to
``Path(os.environ["VIEWLYT_E2E_STATIC"])`` when that variable is set (default:
the package's built ``static/`` dir). Point it at a bundle built from a mutated
copy of ``dashboard/`` to prove these tests actually fail when the UI breaks::

    VIEWLYT_E2E_STATIC=/path/to/static-mutated VIEWLYT_E2E=1 \\
        uv run --with playwright pytest -m e2e tests/test_live_e2e_dashboard.py

Opt-in like the other e2e (``VIEWLYT_E2E=1`` + the 'e2e' dependency group +
``playwright install webkit``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import E2E

pytestmark = [E2E, pytest.mark.e2e]

pytest.importorskip("fastapi")
pw_sync = pytest.importorskip("playwright.sync_api")
expect = pw_sync.expect

from fastapi import FastAPI, Request  # noqa: E402

from viewlyt.live import persistence  # noqa: E402
from viewlyt.live import server as live_server  # noqa: E402
from viewlyt.live.llm import LLMConfig  # noqa: E402
from viewlyt.live.server import LiveServer, create_app  # noqa: E402
from viewlyt.live.window import WindowConfig  # noqa: E402

CHART_TYPES = [
    "bars",
    "columns",
    "stacked",
    "donut",
    "lines",
    "area",
    "delta",
    "gauge",
    "heatmap",
    "podium",
    "violin",
    "descartes",
]
MOOD = {
    "kind": "classification",
    "id": "mood",
    "label": "Mood",
    "question": "How is the crowd feeling?",
    "categories": ["happy", "sad", "neutral"],
    "chart": "bars",
}
GRID = {
    "kind": "classification",
    "id": "grid",
    "label": "Scoreline",
    "question": "Which scoreline do viewers predict?",
    "categories": ["(0, 0)", "(1, 0)", "(0, 1)", "(1, 1)"],
    "chart": "descartes",
}


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app: Any) -> tuple[Any, threading.Thread, int]:
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in 10s")
        time.sleep(0.05)
    return server, thread, port


def _build_fake_llm() -> Any:
    fake = FastAPI()

    @fake.post("/v1/chat/completions")
    async def completions(req: Request) -> dict:
        body = await req.json()
        rf = body.get("response_format") or {}
        schema = (rf.get("json_schema") or {}).get("schema") or {}
        props = schema.get("properties") or {}
        user = str(body["messages"][-1]["content"])
        await asyncio.sleep(0.05)
        if "labels" in props:
            cats = props["labels"]["items"].get("enum") or ["a", "b"]
            n = len(re.findall(r"^\d+\. ", user, flags=re.M)) or 10
            content = json.dumps({"labels": [cats[i % len(cats)] for i in range(n)]})
        elif "summary" in props:
            content = json.dumps({"summary": "The chat is **upbeat** and talks about the stream."})
        else:
            content = json.dumps({"label": "Probe", "instruction": "Summarize", "kind": "open"})
        return {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 900,
                "completion_tokens": 120,
                "total_tokens": 1020,
                "cost": 0.0003,
            },
        }

    return fake


@dataclass
class Live:
    url: str
    port: int
    srv: LiveServer


@contextmanager
def _live_server(fake_base_url: str) -> Iterator[Live]:
    # Unpaused, 1 s refresh: a freshly added probe is analyzed right away
    # (force_now) once the buffer holds messages.
    srv = LiveServer(
        LLMConfig(base_url=fake_base_url, api_key="x", model="fake-model"),
        WindowConfig(gap=1.0, mode="hybrid"),
    )
    srv.paused = False
    server, thread, port = _serve(create_app(srv))
    try:
        yield Live(f"http://127.0.0.1:{port}", port, srv)
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="module", autouse=True)
def _isolated_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Persistence in tmp (never ~/.viewlyt) + the optional mutated-bundle override."""
    state = tmp_path_factory.mktemp("viewlyt-state")
    mp = pytest.MonkeyPatch()
    mp.setattr(persistence, "STATE_DIR", state)
    mp.setattr(persistence, "STATE_FILE", state / "live-state.json")
    mp.setattr(persistence, "KEY_FILE", state / "key")
    static = os.environ.get("VIEWLYT_E2E_STATIC")
    if static:
        mp.setattr(live_server, "STATIC_DIR", Path(static))
    yield
    mp.undo()


@pytest.fixture(scope="module")
def fake_llm() -> Iterator[str]:
    server, thread, port = _serve(_build_fake_llm())
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with pw_sync.sync_playwright() as p:
        b = p.webkit.launch()
        yield b
        b.close()


@pytest.fixture()
def fresh_live(fake_llm: str) -> Iterator[Live]:
    with _live_server(fake_llm) as live:
        yield live


# --------------------------------------------------------------------------- #
# Page helpers
# --------------------------------------------------------------------------- #

CONTROL_JS = """([port, ops]) => new Promise((resolve, reject) => {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/control`);
  ws.onopen = () => {
    for (const op of ops) ws.send(JSON.stringify(op));
    setTimeout(() => { ws.close(); resolve(true); }, 200);
  };
  ws.onerror = () => reject(new Error("control socket failed"));
})"""

INGEST_JS = """([port, count, start, prefix]) => new Promise((resolve, reject) => {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/ingest`);
  ws.onopen = () => {
    const batch = [];
    for (let i = start; i < start + count; i++) {
      batch.push({ author: `user${i}`, text: `${prefix} ${i}`, ts: Date.now() });
    }
    ws.send(JSON.stringify(batch));
    setTimeout(() => { ws.close(); resolve(true); }, 300);
  };
  ws.onerror = () => reject(new Error("ingest socket failed"));
})"""


def control(page: Any, live: Live, *ops: dict) -> None:
    page.evaluate(CONTROL_JS, [live.port, list(ops)])


def ingest(page: Any, live: Live, count: int, *, start: int = 0, prefix: str = "message") -> None:
    page.evaluate(INGEST_JS, [live.port, count, start, prefix])
    expect(page.locator("#ingested")).to_have_text(str(start + count), timeout=10_000)


def card(page: Any, probe_id: str) -> Any:
    return page.locator(f'.result-card[data-probe-id="{probe_id}"]')


def expect_chart(page: Any, probe_id: str) -> Any:
    """Wait for the probe's first result: a chart with a non-empty accessible name."""
    viz = card(page, probe_id).locator(".chart-viz")
    expect(viz).to_be_visible(timeout=15_000)
    expect(viz).not_to_have_attribute("aria-label", "")
    return viz


@contextmanager
def dashboard(browser: Any, live: Live, **ctx: Any) -> Iterator[tuple[Any, list[str]]]:
    """A fresh context + connected page; asserts no console/page errors on exit."""
    ctx.setdefault("viewport", {"width": 1440, "height": 900})
    context = browser.new_context(**ctx)
    errors: list[str] = []
    page = context.new_page()
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    try:
        page.goto(live.url)
        expect(page.locator("#status")).to_have_text("Connected", timeout=10_000)
        yield page, errors
        assert errors == [], f"browser errors: {errors}"
    finally:
        context.close()


def start_probes(page: Any, live: Live, *probes: dict, messages: int = 30) -> None:
    """Ingest first (the buffer must hold messages), then add probes: each runs at once."""
    ingest(page, live, messages)
    control(page, live, *({"op": "upsert_probe", "probe": p} for p in probes))
    for p in probes:
        if p["kind"] == "open":
            expect(card(page, p["id"]).locator(".result-display")).to_contain_text(
                "upbeat", timeout=15_000
            )
        else:
            expect_chart(page, p["id"])


@pytest.fixture(scope="module")
def live(fake_llm: str, browser: Any) -> Iterator[Live]:
    """Shared, pre-seeded server for the read-mostly scenarios: 30 messages, a
    classification probe (``mood``) and an open one (``summary``), both with results."""
    summary = {
        "kind": "open",
        "id": "summary",
        "label": "Summary",
        "instruction": "Summarize the chat.",
    }
    with _live_server(fake_llm) as lv:
        with dashboard(browser, lv) as (page, _):
            start_probes(page, lv, MOOD, summary)
        yield lv


# --------------------------------------------------------------------------- #
# 1. Theme
# --------------------------------------------------------------------------- #


def test_theme_cycles_persists_and_system_clears(browser: Any, live: Live) -> None:
    with dashboard(browser, live) as (page, errors):
        html = page.locator("html")
        toggle = page.locator("#theme-toggle")
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        expect(toggle).to_have_attribute("aria-label", "Theme: System. Switch to Light")

        toggle.click()
        expect(html).to_have_attribute("data-theme", "light")
        expect(toggle).to_have_attribute("aria-label", "Theme: Light. Switch to Dark")
        light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")

        toggle.click()
        expect(html).to_have_attribute("data-theme", "dark")
        expect(toggle).to_have_attribute("aria-label", "Theme: Dark. Switch to System")
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert light_bg != dark_bg, "Light and Dark must actually restyle the page"

        # The choice survives a reload (stored, and applied before first paint).
        page.reload()
        expect(page.locator("#status")).to_have_text("Connected", timeout=10_000)
        expect(html).to_have_attribute("data-theme", "dark")
        expect(toggle).to_have_attribute("aria-label", "Theme: Dark. Switch to System")
        assert page.evaluate("localStorage.getItem('viewlyt.theme')") == "dark"

        # System = no attribute at all (and no stored preference).
        toggle.click()
        expect(html).not_to_have_attribute("data-theme", re.compile(".*"))
        assert page.evaluate("document.documentElement.hasAttribute('data-theme')") is False
        assert page.evaluate("localStorage.getItem('viewlyt.theme')") is None
        expect(toggle).to_have_attribute("aria-label", "Theme: System. Switch to Light")
        page.reload()
        expect(page.locator("#status")).to_have_text("Connected", timeout=10_000)
        assert page.evaluate("document.documentElement.hasAttribute('data-theme')") is False
        assert errors == []


# --------------------------------------------------------------------------- #
# 2. Settings drawer
# --------------------------------------------------------------------------- #


def test_settings_drawer_modal_tabs_esc_and_remembered_tab(browser: Any, live: Live) -> None:
    with dashboard(browser, live) as (page, _):
        opener = page.locator("#settings-open")
        drawer = page.locator("#settings-drawer")
        tabs = {
            name: page.locator(f"#tab-{name}") for name in ("window", "model", "probes", "capture")
        }

        opener.click()
        expect(drawer).to_be_visible()
        assert drawer.evaluate("d => d.open && d.matches(':modal')") is True
        expect(page.get_by_role("dialog", name="Settings")).to_be_visible()
        expect(tabs["window"]).to_have_attribute("aria-selected", "true")
        expect(tabs["window"]).to_be_focused()

        # Roving arrow keys (wraps around): Window -> Model -> Probes -> Capture -> Window.
        order = ["model", "probes", "capture", "window"]
        for name in order:
            page.keyboard.press("ArrowRight")
            expect(tabs[name]).to_have_attribute("aria-selected", "true")
            expect(tabs[name]).to_be_focused()
            for other, tab in tabs.items():
                if other != name:
                    expect(tab).to_have_attribute("aria-selected", "false")
            expect(page.locator(f"#panel-{name}")).to_be_visible()
        page.keyboard.press("ArrowLeft")
        expect(tabs["capture"]).to_have_attribute("aria-selected", "true")
        page.keyboard.press("Home")
        expect(tabs["window"]).to_have_attribute("aria-selected", "true")
        page.keyboard.press("End")
        expect(tabs["capture"]).to_have_attribute("aria-selected", "true")
        page.keyboard.press("ArrowLeft")
        expect(tabs["probes"]).to_have_attribute("aria-selected", "true")

        # Esc closes the modal and hands focus back to the button that opened it.
        page.keyboard.press("Escape")
        expect(drawer).not_to_be_visible()
        assert drawer.evaluate("d => d.open") is False
        expect(opener).to_be_focused()

        # Reopening lands on the last tab, with focus on it.
        opener.click()
        expect(drawer).to_be_visible()
        expect(tabs["probes"]).to_have_attribute("aria-selected", "true")
        expect(tabs["probes"]).to_be_focused()
        expect(page.locator("#panel-probes")).to_be_visible()
        page.keyboard.press("Escape")
        expect(opener).to_be_focused()


# --------------------------------------------------------------------------- #
# 3. Probe lifecycle
# --------------------------------------------------------------------------- #


def _exported_probe_ids(page: Any, live: Live) -> list[str]:
    data = page.request.get(live.url + "/export.json").json()
    return [entry["probe"]["id"] for entry in data["probes"]]


def test_probe_lifecycle_add_menu_collapse_remove(browser: Any, fresh_live: Live) -> None:
    live = fresh_live
    with dashboard(browser, live) as (page, _):
        ingest(page, live, 30)

        page.click("#settings-open")
        page.click("#tab-probes")
        page.select_option("#probe-kind", "classification")
        page.fill("#probe-label", "Vibes")
        page.fill("#probe-question", "How is the crowd feeling?")
        page.fill("#probe-categories", "happy, sad, neutral")
        page.click("#add-probe")
        expect(page.locator(".toast-success")).to_be_visible(timeout=5_000)
        page.keyboard.press("Escape")

        probe = card(page, "vibes")
        expect(probe).to_be_visible()
        viz = expect_chart(page, "vibes")
        expect(viz).to_have_attribute("role", "img")
        expect(page.locator("#probe-count")).to_have_text("1 probe")

        # Overflow menu, keyboard only: Enter opens, arrows move, Enter activates.
        menu_btn = probe.locator(".card-menu-btn")
        menu = probe.locator(".card-menu")

        def open_menu_and_pick(css_class: str) -> None:
            menu_btn.focus()
            page.keyboard.press("Enter")
            expect(menu_btn).to_have_attribute("aria-expanded", "true")
            expect(menu).to_be_visible()
            for _ in range(6):  # bounded walk down to the wanted item
                if page.evaluate(f"document.activeElement.classList.contains('{css_class}')"):
                    break
                page.keyboard.press("ArrowDown")
            expect(probe.locator(f".{css_class}")).to_be_focused()
            page.keyboard.press("Enter")

        open_menu_and_pick("card-collapse")
        expect(probe).to_have_class(re.compile(r"\bcollapsed\b"))
        expect(menu_btn).to_have_attribute("aria-expanded", "false")
        expect(menu_btn).to_be_focused()

        menu_btn.focus()
        page.keyboard.press("Enter")
        expect(probe.locator(".card-collapse .menu-label")).to_have_text("Expand")
        page.keyboard.press("Escape")
        expect(menu).not_to_be_visible()
        open_menu_and_pick("card-collapse")
        expect(probe).not_to_have_class(re.compile(r"\bcollapsed\b"))

        # Remove: gone from the page and from the server's export.
        assert _exported_probe_ids(page, live) == ["vibes"]
        open_menu_and_pick("card-remove")
        expect(probe).to_have_count(0)
        expect(page.locator("#force-run")).to_be_focused()
        deadline = time.monotonic() + 5
        while _exported_probe_ids(page, live) != [] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _exported_probe_ids(page, live) == []


# --------------------------------------------------------------------------- #
# 4. Every chart type
# --------------------------------------------------------------------------- #


def test_every_chart_type_renders_with_an_accessible_name(browser: Any, fresh_live: Live) -> None:
    live = fresh_live
    with dashboard(browser, live) as (page, errors):
        start_probes(page, live, MOOD)
        select = card(page, "mood").locator(".chart-select")
        viz = card(page, "mood").locator(".chart-viz")
        expect(select.locator("option")).to_have_count(len(CHART_TYPES))
        assert select.locator("option").evaluate_all("o => o.map(x => x.value)") == CHART_TYPES

        seen: list[str] = []
        previous = viz.evaluate("v => v.outerHTML")
        for chart in CHART_TYPES[1:] + CHART_TYPES[:1]:  # ends back on "bars"
            select.select_option(chart)
            expect(select).to_have_value(chart)
            expect(viz).to_be_visible()
            expect(viz).not_to_have_attribute("aria-label", "")
            name = viz.get_attribute("aria-label")
            assert name and name.strip(), f"{chart}: empty accessible name"
            assert viz.evaluate("v => v.childElementCount") > 0, f"{chart}: rendered nothing"
            current = viz.evaluate("v => v.outerHTML")
            assert current != previous, f"{chart}: the chart did not change"
            previous = current
            seen.append(chart)
        assert sorted(seen) == sorted(CHART_TYPES)
        # The pick is persisted server-side (re-upserted probe descriptor).
        deadline = time.monotonic() + 5
        while (
            live.srv.probes["mood"].to_dict().get("chart") != "bars" and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        assert errors == []


# --------------------------------------------------------------------------- #
# 5. Decider status + providers
# --------------------------------------------------------------------------- #


def test_decider_status_chip_and_provider_presets(browser: Any, fresh_live: Live) -> None:
    live = fresh_live
    with dashboard(browser, live) as (page, _):
        page.click("#settings-open")
        page.click("#tab-model")
        status = page.locator("#decider-status")
        chip = page.locator("#decider-chip")

        # Provider without a decisions endpoint: configured but inactive.
        control(
            page,
            live,
            {
                "op": "set_model",
                "base_url": "http://localhost:1234/v1",
                "model": "local-model",
                "decider_model": "perplexity/pplx-decider-flash-v1.2",
            },
        )
        expect(status).to_have_attribute("data-state", "inactive")
        expect(page.locator("#decider-status-text")).to_contain_text("inactive")
        expect(chip).to_be_hidden()

        # OpenRouter has a decisions endpoint: active, and the top bar says so.
        control(
            page,
            live,
            {
                "op": "set_model",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "fake-model",
                "decider_model": "perplexity/pplx-decider-flash-v1.2",
            },
        )
        expect(status).to_have_attribute("data-state", "ok")
        expect(page.locator("#decider-status-text")).to_contain_text("Decider active")
        expect(chip).to_be_visible()
        expect(page.locator("#decider-chip-text")).to_have_text("Decider on")

        # Cleared decider: off, indicator gone.
        control(
            page,
            live,
            {
                "op": "set_model",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "fake-model",
                "decider_model": "",
            },
        )
        expect(status).to_have_attribute("data-state", "off")
        expect(page.locator("#decider-status-text")).to_contain_text("Decider off")
        expect(chip).to_be_hidden()

        # Perplexity is a provider preset that fills in its base URL.
        provider = page.locator("#provider")
        expect(provider.locator('option[value="perplexity"]')).to_have_count(1)
        provider.select_option("perplexity")
        expect(page.locator("#base_url")).to_have_value("https://api.perplexity.ai")
        provider.select_option("lmstudio")
        expect(page.locator("#base_url")).to_have_value("http://localhost:1234/v1")


# --------------------------------------------------------------------------- #
# 6. Chat feed
# --------------------------------------------------------------------------- #


def test_chat_feed_new_message_pill_keyboard_jump(browser: Any, fresh_live: Live) -> None:
    live = fresh_live
    with dashboard(browser, live) as (page, _):
        feed = page.locator("#feed")
        jump = page.locator("#feed-jump")
        ingest(page, live, 60)
        expect(feed.locator(".feed-line")).to_have_count(60, timeout=10_000)
        expect(feed).to_contain_text("user0: message 0")
        expect(feed).to_contain_text("user59: message 59")
        expect(jump).to_be_hidden()  # parked at the newest edge: nothing to announce

        # Read upwards: scroll away from the newest edge.
        away = page.evaluate(
            """() => {
              const f = document.getElementById("feed");
              f.scrollTop = 0;
              return f.scrollHeight - f.scrollTop - f.clientHeight;
            }"""
        )
        assert away > 100, f"the feed must overflow to test scrolling (distance {away})"

        ingest(page, live, 5, start=60)
        expect(jump).to_be_visible(timeout=5_000)
        expect(jump).to_contain_text("5 new")
        expect(feed.locator(".feed-line")).to_have_count(65)

        # Keyboard activation hides the pill, lands on the newest edge, keeps focus in the feed.
        jump.focus()
        expect(jump).to_be_focused()
        page.keyboard.press("Enter")
        expect(jump).to_be_hidden()
        expect(feed).to_be_focused()
        gap = page.evaluate(
            "() => { const f = document.getElementById('feed');"
            " return f.scrollHeight - f.scrollTop - f.clientHeight; }"
        )
        assert gap < 40, f"feed not at the newest edge (distance {gap})"
        expect(feed).to_contain_text("user64: message 64")


# --------------------------------------------------------------------------- #
# 7. Mobile
# --------------------------------------------------------------------------- #


def test_mobile_no_horizontal_overflow_and_touch_targets(browser: Any, live: Live) -> None:
    with dashboard(
        browser, live, viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    ) as (page, _):
        expect_chart(page, "mood")
        widths = page.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert widths[0] <= widths[1], f"horizontal overflow: scrollWidth {widths[0]} > {widths[1]}"

        mood = card(page, "mood")
        controls = {
            "settings": page.locator("#settings-open"),
            "theme toggle": page.locator("#theme-toggle"),
            "run control": page.locator("#global-pause"),
            "ask auto": page.locator("#ask-auto"),
            "ask open": page.locator("#ask-open"),
            "ask classify": page.locator("#ask-classify"),
            "ask suggest": page.locator("#ask-suggest"),
            "ask split": page.locator("#ask-split"),
            "ask add": page.locator("#ask-send"),
            "card menu": mood.locator(".card-menu-btn"),
            "chart select": mood.locator(".chart-select"),
        }
        too_small = {}
        for name, loc in controls.items():
            loc.scroll_into_view_if_needed()
            box = loc.bounding_box()
            assert box is not None, f"{name}: not rendered"
            if box["width"] < 44 or box["height"] < 44:
                too_small[name] = (round(box["width"], 1), round(box["height"], 1))
        assert not too_small, f"touch targets under 44x44: {too_small}"


# --------------------------------------------------------------------------- #
# 8. Grid (2D) two-step remove
# --------------------------------------------------------------------------- #


def test_grid_row_remove_needs_a_second_press_and_esc_disarms(
    browser: Any, fresh_live: Live
) -> None:
    live = fresh_live
    with dashboard(browser, live) as (page, _):
        start_probes(page, live, GRID)
        grid = card(page, "grid")
        rows = grid.locator(".descartes-ylabel")
        expect(rows).to_have_count(2)
        remove = grid.locator(".descartes-ylabel .descartes-tick-del").first
        expect(remove).to_have_attribute("aria-label", re.compile(r"^Remove row "))

        # First press only arms: the row stays, the accessible name asks for confirmation.
        remove.focus()
        page.keyboard.press("Enter")
        expect(remove).to_have_attribute("aria-label", re.compile(r"^Confirm remove row "))
        expect(remove).to_have_class(re.compile(r"\bis-armed\b"))
        expect(rows).to_have_count(2)

        # Esc disarms without removing anything.
        page.keyboard.press("Escape")
        expect(remove).to_have_attribute("aria-label", re.compile(r"^Remove row "))
        expect(remove).not_to_have_class(re.compile(r"\bis-armed\b"))
        expect(rows).to_have_count(2)

        # Arm again, then the second press removes the row.
        page.keyboard.press("Enter")
        expect(remove).to_have_attribute("aria-label", re.compile(r"^Confirm remove row "))
        expect(rows).to_have_count(2)
        page.keyboard.press("Enter")
        expect(rows).to_have_count(1)
        cats = getattr(live.srv.probes["grid"], "categories", [])  # a ClassificationProbe
        assert not any(c.endswith(", 1)") for c in cats), cats  # the y=1 row is gone server-side


# --------------------------------------------------------------------------- #
# 9. Reduced motion
# --------------------------------------------------------------------------- #


def test_reduced_motion_leaves_no_running_animations(browser: Any, fresh_live: Live) -> None:
    live = fresh_live
    with dashboard(browser, live, reduced_motion="reduce") as (page, _):
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches") is True
        start_probes(page, live, MOOD)
        expect(page.locator("#spinner")).to_be_hidden(timeout=10_000)  # the analysis is done
        page.wait_for_function("document.getAnimations().length === 0", timeout=5_000)
        assert page.evaluate("document.getAnimations().length") == 0

    # Control: the very same page with motion allowed does animate (so the zero above
    # is the stylesheet honoring the preference, not an idle page).
    with dashboard(browser, live) as (page, _):
        expect_chart(page, "mood")
        page.wait_for_function("document.getAnimations().length > 0", timeout=5_000)


# --------------------------------------------------------------------------- #
# 10. Accessibility smoke
# --------------------------------------------------------------------------- #


def test_accessibility_smoke_landmarks_toasts_and_tab_order(browser: Any, live: Live) -> None:
    with dashboard(browser, live) as (page, _):
        expect_chart(page, "mood")
        expect(page.locator("h1")).to_have_count(1)
        expect(page.get_by_role("main")).to_have_count(1)
        rail = page.get_by_role("complementary", name="Live chat")
        expect(rail).to_have_count(1)
        assert rail.evaluate("e => e.tagName") == "ASIDE"
        expect(page.get_by_role("region", name="Chat messages")).to_have_count(1)

        # The toast stack announces per toast: errors are role=alert, the rest role=status.
        assert page.locator("#toasts").count() == 1
        page.click("#settings-open")
        page.click("#tab-probes")
        page.click("#import-json")  # empty textarea -> info toast
        info = page.locator(".toast-info")
        expect(info.first).to_be_visible()
        assert info.first.get_attribute("role") == "status"
        page.fill("#probe-json", "{ not json")
        page.click("#import-json")  # invalid -> error toast
        error = page.locator(".toast-error")
        expect(error.first).to_be_visible()
        assert error.first.get_attribute("role") == "alert"
        page.keyboard.press("Escape")

        # Tab order: from the top, the composer input comes before any probe card control.
        page.evaluate("() => { document.activeElement && document.activeElement.blur(); }")
        trail: list[str] = []
        for _ in range(40):
            page.keyboard.press("Tab")
            info_ = page.evaluate(
                """() => {
                  const a = document.activeElement;
                  return { id: a.id || a.className || a.tagName, inCard: !!a.closest('.result-card') };
                }"""
            )
            trail.append(info_["id"])
            assert not info_["inCard"], f"reached a probe card control before the composer: {trail}"
            if info_["id"] == "ask-text":
                break
        else:
            pytest.fail(f"Tab never reached the composer input: {trail}")
        assert "ask-text" in trail
