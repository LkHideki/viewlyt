"""FastAPI app for the real-time live-chat analysis loop.

Three WebSocket channels drive the system: ``/ingest`` (the browser snippet pushes
raw chat rows), ``/control`` (the dashboard mutates probes/window/model/state), and
``/dashboard`` (the server streams ``state``/``result``/``stat`` frames out). A single
async :func:`worker` drains the ingest queue, feeds the :class:`WindowBuffer`, and—
whenever a snapshot is due—runs every probe through the LLM and broadcasts the
results. Heavy deps (FastAPI, uvicorn, openai) are imported here / lazily, never in
the pure modules. No Selenium anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .llm import LLMClient, LLMConfig, LLMRunner, run_probes
from .messages import clean_chat, message_from_ingest
from .probes import Probe, probe_from_dict
from .window import WindowBuffer, WindowConfig

logger = logging.getLogger("viewlyt.live")
STATIC_DIR = Path(__file__).parent / "static"


class ConnectionManager:
    """Tracks the open dashboard sockets and fans a message out to all of them."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        """Accept and register a dashboard socket."""
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        """Forget a socket (idempotent)."""
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        """Send ``message`` to every live socket, dropping any that error out."""
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


class LiveServer:
    """Mutable runtime state shared between the routes and the worker."""

    def __init__(self, llm_cfg: LLMConfig, window: WindowConfig) -> None:
        self.llm_cfg = llm_cfg
        self.window = window
        self.probes: dict[str, Probe] = {}
        self.paused = False
        self.ingested = 0
        self.buffer = WindowBuffer()
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=5000)
        self.dash = ConnectionManager()
        self._client: LLMRunner | None = None

    def client(self) -> LLMRunner:
        """Lazily build (and cache) the LLM client for the current config."""
        if self._client is None:
            self._client = LLMClient(self.llm_cfg)
        return self._client

    def state_message(self) -> dict:
        """Full snapshot frame sent to a dashboard on connect / after every control op."""
        return {
            "type": "state",
            "window": self.window.to_dict(),
            "model": self.llm_cfg.to_public_dict(),
            "paused": self.paused,
            "ingested": self.ingested,
            "probes": [p.to_dict() for p in self.probes.values()],
        }


async def apply_control(server: LiveServer, data: dict) -> None:
    """Apply one control op to ``server``, then rebroadcast the new state.

    A malformed op is logged and swallowed so a bad frame never tears down the
    control socket; the fresh ``state`` frame always goes out at the end.
    """
    try:
        op = data.get("op")
        if op == "upsert_probe":
            p = probe_from_dict(data["probe"])
            server.probes[p.id] = p
        elif op == "remove_probe":
            server.probes.pop(str(data.get("id")), None)
        elif op == "set_window":
            merge = dict(server.window.to_dict())
            for k in ("n", "overlap", "gap", "mode", "dedupe", "merge_authors"):
                if k in data:
                    merge[k] = data[k]
            server.window = WindowConfig.from_dict(merge)
        elif op == "set_model":
            server.llm_cfg = LLMConfig(
                base_url=data.get("base_url") or server.llm_cfg.base_url,
                api_key=data.get("api_key") or server.llm_cfg.api_key,
                model=data.get("model") or server.llm_cfg.model,
            )
            server._client = None
        elif op == "pause":
            server.paused = True
        elif op == "resume":
            server.paused = False
        elif op == "clear":
            server.buffer = WindowBuffer()
    except Exception:
        logger.exception("control op failed: %r", data)
    await server.dash.broadcast(server.state_message())


async def broadcast_stat(server: LiveServer, window_n: int = 0) -> None:
    """Push the live counters to every dashboard — called in real time as messages arrive."""
    await server.dash.broadcast(
        {
            "type": "stat",
            "ingested": server.ingested,
            "buffer": len(server.buffer),
            "window": window_n,
        }
    )


async def process_window(server: LiveServer, window: list, now_wall: float) -> None:
    """Run all probes over one snapshot and broadcast each result plus a stat frame."""
    probes = list(server.probes.values())
    if not probes:
        return
    window = clean_chat(
        window, dedupe=server.window.dedupe, merge_authors=server.window.merge_authors
    )
    results = await run_probes(server.client(), probes, window)
    for r in results:
        r.ts = now_wall
        await server.dash.broadcast(r.to_dict())
    if not results:
        # Every probe errored — almost always the LLM endpoint is unreachable.
        await server.dash.broadcast(
            {
                "type": "error",
                "message": (
                    "All probes failed for this batch — is the LLM reachable at "
                    f"{server.llm_cfg.base_url} (model '{server.llm_cfg.model}')? Check the server log."
                ),
            }
        )
    await broadcast_stat(server, len(window))


async def worker(server: LiveServer) -> None:
    """Forever: drain the queue, feed the buffer, and emit snapshots when due.

    Window timing uses the monotonic clock; result timestamps use wall time. Each
    iteration is guarded so a transient failure (e.g. the LLM endpoint) self-heals
    instead of killing the loop.
    """
    last_stat = 0.0
    while True:
        try:
            try:
                msg = await asyncio.wait_for(server.queue.get(), timeout=1.0)
                got = True
            except TimeoutError:
                got = False
            if got:
                server.ingested += 1
                server.buffer.add(msg)
            if server.paused:
                continue
            now = time.monotonic()
            if server.buffer.due(server.window, now):
                w = server.buffer.emit(server.window, now)
                await process_window(server, w, time.time())
                last_stat = now
            elif got and now - last_stat >= 0.25:
                # Real-time feedback so the dashboard's counters climb as messages arrive,
                # not only once a whole window is processed.
                await broadcast_stat(server)
                last_stat = now
        except Exception:
            logger.exception("worker iteration failed")
            continue


SNIPPET_JS = """(function () {
  var WS_URL = "ws://%HOST%:%PORT%/ingest";
  var ws, queue = [], sent = 0, captured = 0, connected = false;
  var badge = document.createElement("div");
  badge.style.cssText = "position:fixed;z-index:2147483647;right:8px;bottom:8px;font:12px/1.4 system-ui,sans-serif;color:#fff;padding:6px 10px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.45);max-width:70vw";
  function paint(text, color) { badge.textContent = "viewlyt: " + text; badge.style.background = color || "#1e3a8a"; }
  (document.body || document.documentElement).appendChild(badge);
  function status() { paint("connected | captured " + captured + " | sent " + sent, "#14532d"); }
  function connect() {
    try { ws = new WebSocket(WS_URL); } catch (e) { paint("cannot open socket", "#7f1d1d"); return; }
    ws.onopen = function () { connected = true; console.log("[viewlyt] connected", WS_URL); while (queue.length && ws.readyState === 1) { ws.send(queue.shift()); sent++; } status(); };
    ws.onclose = function () { connected = false; paint("disconnected, retrying...", "#78350f"); setTimeout(connect, 2000); };
    ws.onerror = function () { connected = false; paint("CANNOT REACH SERVER - see console", "#7f1d1d"); console.warn("[viewlyt] WebSocket to " + WS_URL + " failed. Usual causes: (1) accept Chrome's one-time 'local network' permission; (2) an ad blocker blocking 127.0.0.1 (uBlock Origin: turn off 'Block outsider intrusion into LAN', or allowlist this page). The separate 'ad_break ERR_BLOCKED_BY_CLIENT' line is just your ad blocker and does NOT affect viewlyt."); };
  }
  function send(obj) { var s = JSON.stringify(obj); if (ws && ws.readyState === 1) { ws.send(s); sent++; } else { queue.push(s); if (queue.length > 2000) queue.shift(); } if (connected) status(); }
  function findItems(doc) { return doc.querySelector("yt-live-chat-item-list-renderer #items") || doc.querySelector("#items"); }
  function emit(node) { if (!node || !node.querySelector) return; var m = node.querySelector("#message"); if (!m || !m.innerHTML) return; var a = node.querySelector("#author-name"); captured++; send({ type: "msg", author: a ? a.textContent.trim() : "", html: m.innerHTML, ts: Date.now() }); }
  function handle(node) { if (!node || !node.querySelector) return; var m = node.querySelector("#message"); if (m && m.innerHTML) emit(node); else setTimeout(function () { emit(node); }, 0); }
  function backfill(items) { var ex = items.querySelectorAll("yt-live-chat-text-message-renderer, yt-live-chat-paid-message-renderer"); for (var j = Math.max(0, ex.length - 50); j < ex.length; j++) emit(ex[j]); }
  function attach(items) { if (!items) { paint("CHAT NOT FOUND - use the popout", "#7f1d1d"); console.warn("[viewlyt] chat #items not found. Open the chat POPOUT (live_chat?is_popout=1) and run this in ITS console, or use the bookmarklet."); return; } backfill(items); new MutationObserver(function (muts) { muts.forEach(function (mut) { for (var i = 0; i < mut.addedNodes.length; i++) handle(mut.addedNodes[i]); }); }).observe(items, { childList: true }); console.log("[viewlyt] attached; backfilled " + captured + " existing messages"); status(); }
  connect();
  var items = findItems(document);
  if (!items) { try { var f = document.querySelector("#chatframe"); if (f && f.contentDocument) items = findItems(f.contentDocument); } catch (e) {} }
  attach(items);
})();"""


def render_snippet(host: str, port: int) -> str:
    """Bind the snippet template to a concrete ``host``/``port``."""
    return SNIPPET_JS.replace("%HOST%", host).replace("%PORT%", str(port))


_NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>viewlyt.live</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto; line-height: 1.5;">
<h1>viewlyt.live</h1>
<p>The dashboard hasn't been built yet. Build it once with:</p>
<pre style="background:#f4f4f5;padding:1rem;border-radius:.5rem;overflow:auto;">npm --prefix src/viewlyt/live/dashboard install
npm --prefix src/viewlyt/live/dashboard run build</pre>
<p>Then reload this page.</p>
</body></html>"""


def create_app(server: LiveServer) -> FastAPI:
    """Build the FastAPI app: lifespan-managed worker, routes, then the static mount."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(worker(server))
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/snippet.js")
    async def snippet_js(request: Request) -> Response:
        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or 8000
        return Response(render_snippet(host, port), media_type="application/javascript")

    @app.get("/")
    async def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return HTMLResponse(page.read_text(encoding="utf-8"))
        return HTMLResponse(_NOT_BUILT_HTML)

    @app.websocket("/ingest")
    async def ingest(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                m = message_from_ingest(data)
                if m is not None:
                    try:
                        server.queue.put_nowait(m)
                    except asyncio.QueueFull:
                        pass
        except (WebSocketDisconnect, Exception):
            return

    @app.websocket("/dashboard")
    async def dashboard(ws: WebSocket) -> None:
        await server.dash.connect(ws)
        try:
            await ws.send_json(server.state_message())
            while True:
                await ws.receive_text()  # keepalive pings, ignored
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            server.dash.disconnect(ws)

    @app.websocket("/control")
    async def control(ws: WebSocket) -> None:
        await ws.accept()
        try:
            while True:
                data = await ws.receive_json()
                await apply_control(server, data)
        except (WebSocketDisconnect, Exception):
            return

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

    return app


def run(
    host: str = "127.0.0.1",
    port: int = 8000,
    *,
    llm_cfg: LLMConfig | None = None,
    window: WindowConfig | None = None,
    open_browser: bool = False,
) -> None:
    """Build the app and serve it with uvicorn (blocking)."""
    import uvicorn

    server = LiveServer(llm_cfg or LLMConfig(), window or WindowConfig())
    app = create_app(server)
    if open_browser:
        try:
            webbrowser.open("http://" + host + ":" + str(port) + "/")
        except Exception:
            pass
    uvicorn.run(app, host=host, port=port, log_level="warning")
