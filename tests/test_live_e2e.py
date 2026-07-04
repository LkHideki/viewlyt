"""Browser e2e for the live dashboard on WEBKIT — Safari's engine.

Boots the real FastAPI/uvicorn server in-process (LLM pointed at a dead port, so
analyses fail fast and deterministically) and drives the built dashboard with
Playwright WebKit: connect, ingest → feed/counters, add a probe through the UI,
watch the error toast when the LLM is unreachable, and download the exports.

Opt-in twice over: set ``VIEWLYT_E2E=1`` (same gate as the Selenium e2e) and have
the 'e2e' dependency group installed::

    uv sync --extra live --group e2e
    uv run playwright install webkit
    VIEWLYT_E2E=1 uv run pytest -m e2e tests/test_live_e2e.py
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from conftest import E2E

pytestmark = [E2E, pytest.mark.e2e]

pytest.importorskip("fastapi")
pw_sync = pytest.importorskip("playwright.sync_api")

from viewlyt.live import persistence  # noqa: E402
from viewlyt.live.llm import LLMConfig  # noqa: E402
from viewlyt.live.server import LiveServer, create_app  # noqa: E402
from viewlyt.live.window import WindowConfig  # noqa: E402


@pytest.fixture()
def live_url(tmp_path, monkeypatch):
    """A real uvicorn serving the real app on a free port; never touches ~/.viewlyt."""
    import uvicorn

    monkeypatch.setattr(persistence, "STATE_DIR", tmp_path)
    monkeypatch.setattr(persistence, "STATE_FILE", tmp_path / "live-state.json")
    monkeypatch.setattr(persistence, "KEY_FILE", tmp_path / "key")

    # base_url on a dead port: any probe analysis fails fast (connection refused),
    # which is exactly what the error-toast assertion needs.
    srv = LiveServer(
        LLMConfig(base_url="http://127.0.0.1:9/v1", api_key="", model="none"),
        WindowConfig(gap=1.0, mode="hybrid"),
    )
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(create_app(srv), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start in 10s")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


INGEST_JS = """(port) => new Promise((resolve, reject) => {
  const ws = new WebSocket(`ws://127.0.0.1:${port}/ingest`);
  ws.onopen = () => {
    ws.send(JSON.stringify([
      { author: "alice", html: "hello from webkit", ts: Date.now() },
      { author: "bob", text: "second message", ts: Date.now() },
    ]));
    setTimeout(() => { ws.close(); resolve(true); }, 300);
  };
  ws.onerror = () => reject(new Error("ingest socket failed"));
})"""


def test_dashboard_full_flow_on_webkit(live_url: str) -> None:
    port = live_url.rsplit(":", 1)[1]
    with pw_sync.sync_playwright() as p:
        browser = p.webkit.launch()
        page = browser.new_page()
        page.goto(live_url)

        # 1. The /dashboard WebSocket connects (this is the Safari-critical path).
        status = page.locator("#status")
        pw_sync.expect(status).to_have_text("Connected", timeout=10_000)

        # 2. Ingest over WS from the page context -> worker -> feed + counters.
        page.evaluate(INGEST_JS, port)
        pw_sync.expect(page.locator("#feed")).to_contain_text("alice:", timeout=10_000)
        pw_sync.expect(page.locator("#feed")).to_contain_text("second message")
        pw_sync.expect(page.locator("#ingested")).to_have_text("2")
        # The feed's empty-state hint is gone once real lines arrive.
        assert page.locator("#feed .empty-hint").count() == 0

        # 3. Add a probe through the UI: success toast + card appear...
        page.fill("#probe-label", "Mood")
        page.fill("#probe-instruction", "Summarize the mood.")
        page.click("#add-probe")
        pw_sync.expect(page.locator(".toast-success")).to_be_visible(timeout=5_000)
        pw_sync.expect(page.locator('.result-card[data-probe-id="mood"]')).to_be_visible()

        # ...and since the LLM endpoint is a dead port, the forced first analysis
        # fails fast and must SAY so (error toast with role=alert).
        error_toast = page.locator(".toast-error")
        pw_sync.expect(error_toast.first).to_be_visible(timeout=15_000)
        assert error_toast.first.get_attribute("role") == "alert"

        # 4. Export endpoints respond with the session (probe included).
        data = page.request.get(live_url + "/export.json").json()
        assert [entry["probe"]["id"] for entry in data["probes"]] == ["mood"]
        csv_text = page.request.get(live_url + "/export.csv").text()
        assert csv_text.startswith("ts_utc,probe_id,")

        browser.close()
