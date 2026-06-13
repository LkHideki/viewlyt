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
import io
import json
import logging
import time
import webbrowser
import zipfile
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
        self.processing = False
        self.last_latency_ms: int | None = None
        self.ingested = 0
        self.buffer = WindowBuffer(maxlen=window.capacity)
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
            "latency_ms": self.last_latency_ms,
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
            old_capacity = server.window.capacity
            merge = dict(server.window.to_dict())
            for k in ("n", "overlap", "gap", "mode", "capacity", "dedupe", "merge_authors"):
                if k in data:
                    merge[k] = data[k]
            server.window = WindowConfig.from_dict(merge)
            if server.window.capacity != old_capacity:
                # Capacity changed: a fresh rolling buffer with the new maxlen.
                server.buffer = WindowBuffer(maxlen=server.window.capacity)
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
    cleaned = clean_chat(window, dedupe=server.window.dedupe, merge_authors=server.window.merge_authors)
    window = cleaned[-server.window.n :]
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


async def _run_window(server: LiveServer, window: list, now_wall: float) -> None:
    """Run one snapshot through the probes, always clearing the ``processing`` guard.

    Brackets the analysis with ``proc`` frames so the dashboard can show a live
    'analyzing…' indicator and the latency of the last batch.
    """
    await server.dash.broadcast({"type": "proc", "active": True})
    t0 = time.monotonic()
    try:
        await process_window(server, window, now_wall)
    finally:
        server.processing = False
        server.last_latency_ms = round((time.monotonic() - t0) * 1000)
        await server.dash.broadcast(
            {"type": "proc", "active": False, "latency_ms": server.last_latency_ms}
        )


async def worker(server: LiveServer) -> None:
    """Forever: drain the queue, feed the buffer, and emit snapshots when due.

    The LLM call is fired off as a background task (guarded by ``server.processing``
    so at most one probe batch runs at a time), so the drain/flush loop keeps mirroring
    the feed and settling the counters every ~0.25s even while an analysis is in flight.
    Window timing uses the monotonic clock; result timestamps use wall time. Each
    iteration is guarded so a transient failure (e.g. the LLM endpoint) self-heals
    instead of killing the loop.
    """
    pending: list[dict] = []
    last_flush = 0.0
    last_ingested = -1
    last_analyzed = -1
    while True:
        try:
            try:
                msg = await asyncio.wait_for(server.queue.get(), timeout=0.25)
                got = True
            except TimeoutError:
                got = False
            if got:
                server.ingested += 1
                server.buffer.add(msg)
                # Mirror every message to the dashboard so the bridge is visibly working,
                # independent of windowing or the LLM — but batched (flushed below) so the
                # dashboard gets at most ~4 feed frames per second regardless of chat speed.
                pending.append({"author": msg.author, "text": msg.text})
                if len(pending) > 5000:
                    # Safety bound only; normal operation flushes every 0.25s, never near this.
                    pending = pending[-5000:]
            now = time.monotonic()
            # Emit a window only when it is due AND new messages arrived since the last
            # analysis — so an idle/quiet chat never triggers a paid LLM request.
            if (
                not server.paused
                and not server.processing
                and server.ingested != last_analyzed
                and server.buffer.due(server.window, now)
            ):
                server.buffer.emit(server.window, now)  # reset windowing timers only
                raw = server.buffer.snapshot()
                last_analyzed = server.ingested
                server.processing = True
                # NON-BLOCKING: the LLM call runs in the background so the loop keeps
                # draining the queue and flushing the feed while it works.
                asyncio.create_task(_run_window(server, raw, time.time()))
            if now - last_flush >= 0.25:
                # Flush the batched feed + settle the counters ~4 times a second.
                if pending:
                    await server.dash.broadcast({"type": "chat", "items": pending})
                    pending = []
                if server.ingested != last_ingested:
                    await broadcast_stat(server)
                    last_ingested = server.ingested
                last_flush = now
        except Exception:
            logger.exception("worker iteration failed")
            continue


SNIPPET_JS = """(function () {
  var WS_URL = "ws://%HOST%:%PORT%/ingest";
  var ws, sent = 0, captured = 0, connected = false, pinger, flusher, scrollTimer, lastCapMs = 0, outbox = [];
  var badge = document.createElement("div");
  badge.style.cssText = "position:fixed;z-index:2147483647;right:8px;bottom:8px;font:12px/1.4 system-ui,sans-serif;color:#fff;padding:6px 10px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.45);max-width:70vw";
  function paint(text, color) { badge.textContent = "viewlyt: " + text; badge.style.background = color || "#1e3a8a"; }
  (document.body || document.documentElement).appendChild(badge);
  function status() { paint("connected | captured " + captured + " | sent " + sent, "#14532d"); }
  function connect() {
    try { ws = new WebSocket(WS_URL); } catch (e) { paint("cannot open socket", "#7f1d1d"); return; }
    ws.onopen = function () { connected = true; console.log("[viewlyt] connected", WS_URL); status(); if (pinger) clearInterval(pinger); pinger = setInterval(function () { if (ws && ws.readyState === 1) ws.send('{"type":"ping"}'); }, 15000); };
    ws.onclose = function () { connected = false; if (pinger) clearInterval(pinger); paint("disconnected, retrying...", "#78350f"); setTimeout(connect, 2000); };
    ws.onerror = function () { connected = false; paint("CANNOT REACH SERVER - see console", "#7f1d1d"); console.warn("[viewlyt] WebSocket to " + WS_URL + " failed. Usual causes: (1) accept Chrome's one-time 'local network' permission; (2) an ad blocker blocking 127.0.0.1 (uBlock Origin: turn off 'Block outsider intrusion into LAN', or allowlist this page). The separate 'ad_break ERR_BLOCKED_BY_CLIENT' line is just your ad blocker and does NOT affect viewlyt."); };
  }
  function send(obj) { outbox.push(obj); if (outbox.length > 3000) outbox.shift(); }
  function flush() { if (!outbox.length || !ws || ws.readyState !== 1) return; var n = outbox.length; ws.send(JSON.stringify(outbox.splice(0, n))); sent += n; if (connected) status(); }
  function findItems(doc) { try { return doc.querySelector("yt-live-chat-item-list-renderer #items") || doc.querySelector("yt-live-chat-renderer #items") || doc.querySelector("#chat #items") || null; } catch (e) { return null; } }
  function chatDoc() { try { var f = document.querySelector("iframe#chatframe, iframe[src*='live_chat']"); if (f && f.contentDocument) return f.contentDocument; } catch (e) {} return null; }
  function locate() { var it = findItems(document); if (it) return it; var cd = chatDoc(); return cd ? findItems(cd) : null; }
  function emit(node) { if (!node || !node.querySelector) return; var m = node.querySelector("#message") || node.querySelector("yt-formatted-string#message") || node.querySelector("#content #message"); if (!m || !m.innerHTML) return; var a = node.querySelector("#author-name"); captured++; lastCapMs = Date.now(); send({ type: "msg", author: a ? a.textContent.trim() : "", html: m.innerHTML, ts: Date.now() }); }
  function handle(node) { if (!node || !node.querySelector) return; var m = node.querySelector("#message"); if (m && m.innerHTML) emit(node); else setTimeout(function () { emit(node); }, 0); }
  function backfill(root) { var ex = root.querySelectorAll("yt-live-chat-text-message-renderer, yt-live-chat-paid-message-renderer, yt-live-chat-membership-item-renderer"); for (var j = Math.max(0, ex.length - 50); j < ex.length; j++) emit(ex[j]); }
  function diag() { if (captured > 0) return; var it = locate(); var here = document.querySelectorAll("yt-live-chat-text-message-renderer").length; var cd = chatDoc(); var inFrame = cd ? cd.querySelectorAll("yt-live-chat-text-message-renderer").length : -1; console.warn("[viewlyt] captured 0. items=" + (it ? it.tagName + " children=" + it.childElementCount : "NULL") + " | renderers_here=" + here + " | renderers_in_chat_iframe=" + inFrame + " | location=" + location.pathname + " -- If renderers>0 but captured 0, send this line to the dev. If all 0/NULL, this page has no live chat: open the POPOUT (live_chat?is_popout=1) of a CURRENTLY-LIVE stream."); paint("captured 0 - open console for diagnostics", "#7f1d1d"); }
  function stickBottom(doc) { try { var sc = doc.querySelector("#item-scroller"); if (!sc) { var it = doc.querySelector("yt-live-chat-item-list-renderer #items"); sc = it && it.parentElement; } if (sc) sc.scrollTop = sc.scrollHeight + 99999; } catch (e) {} }
  function attach(items) { if (!items) { paint("CHAT NOT FOUND - use the popout (live_chat?is_popout=1)", "#7f1d1d"); console.warn("[viewlyt] live chat not found on this page. Open the chat POPOUT and run this there (console or bookmarklet)."); setTimeout(diag, 1000); return; } backfill(items); new MutationObserver(function (muts) { muts.forEach(function (mut) { for (var i = 0; i < mut.addedNodes.length; i++) handle(mut.addedNodes[i]); }); }).observe(items, { childList: true }); var d = items.ownerDocument || document; if (scrollTimer) clearInterval(scrollTimer); scrollTimer = setInterval(function () { if (Date.now() - lastCapMs > 4000) stickBottom(d); }, 3000); stickBottom(d); console.log("[viewlyt] attached to chat; backfilled " + captured + " existing messages"); status(); setTimeout(diag, 4000); setTimeout(diag, 12000); }
  connect();
  flusher = setInterval(flush, 500);
  var tries = 0;
  (function waitForChat() { var it = locate(); if (it) { attach(it); } else if (++tries > 60) { attach(null); } else { setTimeout(waitForChat, 1000); } })();
})();"""


def render_snippet(host: str, port: int) -> str:
    """Bind the snippet template to a concrete ``host``/``port``."""
    return SNIPPET_JS.replace("%HOST%", host).replace("%PORT%", str(port))


def render_userscript(host: str, port: int) -> str:
    """Wrap the snippet in a Tampermonkey/Violentmonkey metadata block (reliable injection)."""
    meta = (
        "// ==UserScript==\n"
        "// @name         viewlyt capture\n"
        "// @namespace    viewlyt.live\n"
        "// @version      1.0\n"
        "// @description  Stream this YouTube live chat to your local viewlyt server\n"
        "// @match        https://www.youtube.com/live_chat*\n"
        "// @grant        none\n"
        "// @run-at       document-idle\n"
        "// ==/UserScript==\n"
    )
    return meta + "\n" + render_snippet(host, port)


MANIFEST_JSON = """{
  "manifest_version": 3,
  "name": "viewlyt capture",
  "version": "1.0",
  "description": "Stream a YouTube live chat to your local viewlyt server",
  "content_scripts": [
    { "matches": ["https://www.youtube.com/live_chat*"], "js": ["content.js"], "run_at": "document_idle", "all_frames": true }
  ]
}"""


def build_extension_zip(host: str, port: int) -> bytes:
    """Pack the MV3 extension (manifest + content.js) into an in-memory zip."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", MANIFEST_JSON)
        zf.writestr("content.js", render_snippet(host, port))
    return buf.getvalue()


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

    @app.middleware("http")
    async def _allow_private_network(request: Request, call_next):
        # Let a page on https://youtube.com reach this loopback server: answer the
        # Private Network Access preflight and tag responses so Chrome is less likely
        # to block the ws://127.0.0.1 connection.
        if request.method == "OPTIONS":
            resp: Response = Response(status_code=200)
        else:
            resp = await call_next(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        return resp

    @app.get("/snippet.js")
    async def snippet_js(request: Request) -> Response:
        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or 8000
        return Response(render_snippet(host, port), media_type="application/javascript")

    @app.get("/viewlyt.user.js")
    async def viewlyt_user_js(request: Request) -> Response:
        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or 8000
        return Response(render_userscript(host, port), media_type="application/javascript")

    @app.get("/viewlyt-extension.zip")
    async def viewlyt_extension_zip(request: Request) -> Response:
        host = request.url.hostname or "127.0.0.1"
        port = request.url.port or 8000
        return Response(
            build_extension_zip(host, port),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="viewlyt-extension.zip"'},
        )

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
                raw = await ws.receive_text()
                try:
                    data = json.loads(raw)
                except Exception:
                    continue  # skip one malformed frame instead of tearing down the bridge
                items = data if isinstance(data, list) else [data]
                for item in items:
                    m = message_from_ingest(item)
                    if m is not None:
                        try:
                            server.queue.put_nowait(m)
                        except asyncio.QueueFull:
                            pass
        except WebSocketDisconnect:
            return
        except Exception:
            logger.exception("ingest socket error")
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
