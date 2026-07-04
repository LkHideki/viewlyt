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
import csv
import io
import json
import logging
import re
import time
import webbrowser
import zipfile
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import persistence
from .llm import (
    LLMClient,
    LLMConfig,
    LLMRunner,
    rewrite_probe_spec,
    run_probes,
    suggest_probes,
)
from .messages import message_from_ingest
from .probes import Probe, probe_from_dict
from .window import WindowBuffer, WindowConfig

logger = logging.getLogger("viewlyt.live")
STATIC_DIR = Path(__file__).parent / "static"

# A dashboard tab that can't take a frame within this window is dropped (it
# reconnects on its own); without it one hung socket would stall the worker loop.
_SEND_TIMEOUT = 5.0

# Snapshots kept per probe, server-side, for reconnect backfill and export.
_HISTORY_CAP = 120


def _origin_allowed(origin: str | None, host: str, port: int, *, allow_youtube: bool) -> bool:
    """Decide whether a WebSocket handshake's ``Origin`` may connect (anti-CSWSH).

    A browser ALWAYS sends ``Origin``; without this check a malicious tab could open
    ``ws://127.0.0.1:<port>/control`` and reconfigure the server (e.g. redirect the LLM
    ``base_url`` to exfiltrate the stored API key, or burn it with forced runs). So:

    * a **missing** Origin is allowed — that's a non-browser client (curl, a local
      script); it cannot mount a CSWSH attack and carries no ambient credentials;
    * a **same-origin** http Origin (the dashboard page this server itself serves) is allowed;
    * for ``/ingest`` only, a ``https://*.youtube.com`` Origin is allowed (the chat
      popout / capture snippet legitimately connect from there);
    * anything else is rejected.
    """
    if not origin:
        return True
    try:
        u = urlsplit(origin)
    except Exception:
        return False
    oh = (u.hostname or "").lower()
    oport = u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "http" and oport == port and oh in {host.lower(), "127.0.0.1", "localhost"}:
        return True
    if (
        allow_youtube
        and u.scheme == "https"
        and (oh == "youtube.com" or oh.endswith(".youtube.com"))
    ):
        return True
    return False


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
        """Send ``message`` to every live socket, dropping any that errors or stalls.

        The frame is serialized ONCE (``send_json`` would re-dump it per socket) and
        the sends run concurrently with a timeout, so one slow or hung tab neither
        delays the other dashboards nor stalls the worker loop calling this ~4×/s.
        """
        if not self.active:
            return
        payload = json.dumps(message)
        conns = list(self.active)
        results = await asyncio.gather(
            *(asyncio.wait_for(ws.send_text(payload), _SEND_TIMEOUT) for ws in conns),
            return_exceptions=True,
        )
        for ws, res in zip(conns, results, strict=True):
            if isinstance(res, BaseException):
                self.disconnect(ws)
                # Best-effort close so the tab sees a clean disconnect and its
                # auto-reconnect kicks in (instead of a zombie socket that just
                # stops receiving frames).
                asyncio.create_task(self._close_quietly(ws))

    @staticmethod
    async def _close_quietly(ws: WebSocket) -> None:
        try:
            await asyncio.wait_for(ws.close(), 2.0)
        except Exception:
            pass


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
        self.total_tokens = 0
        self.total_cost = 0.0
        self.force_now = False  # set by the 'force_run' op → worker analyzes immediately
        self.budget_blocked = False  # True once the budget-cap error has been broadcast
        self.buffer = WindowBuffer(maxlen=window.capacity)
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=5000)
        self.dash = ConnectionManager()
        # Per-probe result snapshots (oldest → newest, bounded): the source for the
        # reconnect backfill and the /export endpoints. A page reload no longer
        # loses the session's analysis history.
        self.history: dict[str, deque[dict]] = {}
        self._client: LLMRunner | None = None

    def client(self) -> LLMRunner:
        """Lazily build (and cache) the LLM client for the current config."""
        if self._client is None:
            self._client = LLMClient(self.llm_cfg)
        return self._client

    def state_message(self) -> dict:
        """Full snapshot frame sent to a dashboard on connect / after every control op.

        Carries the runtime flags too (``processing``/``budget_blocked``/totals), so a
        dashboard that connects mid-analysis or after a budget stop shows the truth
        instead of waiting for the next transient frame.
        """
        return {
            "type": "state",
            "window": self.window.to_dict(),
            "model": self.llm_cfg.to_public_dict(),
            "paused": self.paused,
            "processing": self.processing,
            "budget_blocked": self.budget_blocked,
            "ingested": self.ingested,
            "latency_ms": self.last_latency_ms,
            "tokens_total": self.total_tokens,
            "cost_total": round(self.total_cost, 6),
            "probes": [p.to_dict() for p in self.probes.values()],
        }

    def history_message(self) -> dict:
        """Backfill frame with every probe's stored snapshots (sent once on connect)."""
        return {
            "type": "history",
            "probes": {pid: list(h) for pid, h in self.history.items() if h},
        }


async def persist(server: LiveServer) -> None:
    """Snapshot the server's persisted state to disk off the event loop."""
    await asyncio.to_thread(
        persistence.save_state,
        server.window.to_dict(),
        {
            "base_url": server.llm_cfg.base_url,
            "model": server.llm_cfg.model,
            "api_key": server.llm_cfg.api_key,
            "budget": server.llm_cfg.budget_usd,
            "language": server.llm_cfg.language,
        },
        [p.to_dict() for p in server.probes.values()],
    )


async def apply_control(server: LiveServer, data: dict) -> None:
    """Apply one control op to ``server``, then rebroadcast the new state.

    A malformed op is logged and swallowed so a bad frame never tears down the
    control socket; the fresh ``state`` frame always goes out at the end. State-
    changing ops (probes/window/model) are also persisted to disk before the
    broadcast; the transient ops (pause/resume/clear) are not.
    """
    try:
        op = data.get("op")
        if op == "upsert_probe":
            p = probe_from_dict(data["probe"])
            is_new = p.id not in server.probes
            server.probes[p.id] = p
            if is_new:
                # A brand-new probe is analyzed on the next loop iteration instead of
                # waiting for the refresh timer (no-op while the buffer is still empty).
                server.force_now = True
        elif op == "rewrite_probe":
            # Off-loop: an LLM rewrites the casual ask into a full probe spec, then
            # the task itself stores/persists/broadcasts (so we don't block here and
            # the trailing state frame below reflects the pre-rewrite state).
            asyncio.create_task(
                _rewrite_and_add(
                    server,
                    str(data.get("kind") or "open"),
                    str(data.get("text") or ""),
                    list(data.get("categories") or []),
                )
            )
        elif op == "remove_probe":
            server.probes.pop(str(data.get("id")), None)
            server.history.pop(str(data.get("id")), None)
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
                budget_usd=float(data.get("budget", server.llm_cfg.budget_usd)),
                language=str(data.get("language") or server.llm_cfg.language),
            )
            server._client = None
        elif op == "pause":
            server.paused = True
        elif op == "resume":
            server.paused = False
        elif op == "force_run":
            # Analyze the current buffer immediately, bypassing the refresh timer.
            server.force_now = True
        elif op == "suggest_probes":
            # Off-loop: an LLM proposes two probes from the typed text + live sample,
            # then the task itself broadcasts the 'suggestions' frame (not persisted).
            asyncio.create_task(_suggest(server, str(data.get("text") or "")))
        elif op == "clear":
            server.buffer = WindowBuffer()
        elif op == "reset_state":
            server.probes.clear()
            server.history.clear()
            server.window = WindowConfig()
            server.llm_cfg = LLMConfig()
            server.buffer = WindowBuffer(maxlen=server.window.capacity)
            server._client = None
            server.total_tokens = 0
            server.total_cost = 0.0
            server.budget_blocked = False
            try:
                persistence.STATE_FILE.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        # Redact the api_key before logging — a failed set_model op carries it in `data`.
        safe = (
            {k: ("***" if k == "api_key" else v) for k, v in data.items()}
            if isinstance(data, dict)
            else data
        )
        logger.exception("control op failed: %r", safe)
    if op in {"upsert_probe", "remove_probe", "set_window", "set_model"}:
        await persist(server)
    await server.dash.broadcast(server.state_message())


def _slugify_probe_id(server: LiveServer, label: str) -> str:
    """Turn a label into a unique probe id: lowercase, non-alnum → '-', strip '-'.

    Empty slugs fall back to ``rewrite-<count+1>``; collisions with an existing
    probe id are disambiguated by suffixing ``-2``, ``-3``, ...
    """
    base = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    if not base:
        base = f"rewrite-{len(server.probes) + 1}"
    if base not in server.probes:
        return base
    n = 2
    while f"{base}-{n}" in server.probes:
        n += 1
    return f"{base}-{n}"


async def _rewrite_and_add(server: LiveServer, kind: str, text: str, categories: list[str]) -> None:
    """LLM-rewrite the ask-bar text into a probe, store it, persist, and broadcast.

    Runs as its own task (never raises out of it). On any failure it falls back to
    a raw probe built straight from ``text`` and also emits an ``error`` frame so
    the dashboard tells the user their text was added as-is.
    """
    try:
        try:
            spec = await rewrite_probe_spec(server.client(), kind, text, categories)
            # In "auto" mode the model chose the kind, so the spec carries a "kind"
            # key we pop and honor; explicit kinds keep the requested one.
            resolved_kind = str(spec.pop("kind", kind) if kind == "auto" else kind)
            probe_id = _slugify_probe_id(server, str(spec.get("label") or ""))
            probe_dict = {"kind": resolved_kind, "id": probe_id, **spec}
            if (
                resolved_kind == "classification"
                and not probe_dict.get("categories")
                and categories
            ):
                probe_dict["categories"] = categories
        except Exception:
            logger.exception("probe rewrite failed; adding raw text")
            probe_id = _slugify_probe_id(server, text)
            if kind == "classification":
                probe_dict = {
                    "kind": "classification",
                    "id": probe_id,
                    "label": "",
                    "question": text,
                    "categories": categories or [],
                }
            else:
                probe_dict = {
                    "kind": "open",
                    "id": probe_id,
                    "label": "",
                    "instruction": text,
                }
            await server.dash.broadcast(
                {
                    "type": "error",
                    "message": "Probe rewrite failed; added your text as-is.",
                }
            )
        p = probe_from_dict(probe_dict)
        server.probes[p.id] = p
        server.force_now = True  # analyze the just-created probe right away
        await persist(server)
        await server.dash.broadcast(server.state_message())
    except Exception:
        logger.exception("rewrite task failed entirely for %r", text)


async def _suggest(server: LiveServer, text: str) -> None:
    """LLM-propose two probes from ``text`` + the live sample, then broadcast them.

    Runs as its own task (never raises out of it). Samples the buffer the same way
    :func:`process_window` does (clean → last ``window.n``), asks the LLM for two
    full probe specs, assigns each a unique slug id, and emits a ``suggestions``
    frame the dashboard renders as clickable chips. On empty/failed suggestions it
    emits an ``error`` frame instead. NOT persisted — the user picks a chip to add.
    """
    try:
        sample = server.buffer.sample(server.window)
        try:
            specs = await suggest_probes(server.client(), text, sample)
        except Exception:
            logger.exception("suggest_probes failed for %r", text)
            specs = []
        assigned: set[str] = set()
        for spec in specs:
            pid = _slugify_probe_id(server, str(spec.get("label") or ""))
            # The specs aren't added to server.probes, so also keep the two ids
            # distinct from each other (same label → bump the second).
            if pid in assigned:
                n = 2
                while f"{pid}-{n}" in assigned or f"{pid}-{n}" in server.probes:
                    n += 1
                pid = f"{pid}-{n}"
            assigned.add(pid)
            spec["id"] = pid
        if specs:
            await server.dash.broadcast({"type": "suggestions", "probes": specs})
        else:
            await server.dash.broadcast(
                {"type": "error", "message": "Could not suggest probes right now."}
            )
    except Exception:
        logger.exception("suggest task failed entirely for %r", text)


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
    """Run all probes over one cleaned sample and broadcast each result plus a stat frame.

    ``window`` arrives already sampled/cleaned (:meth:`WindowBuffer.sample`), captured
    synchronously at emit time so it is immune to messages arriving mid-analysis.
    """
    probes = list(server.probes.values())
    if not probes:
        return
    # Snapshot the client's cumulative usage so we can attribute this batch's spend.
    c = server.client()
    before_tok = getattr(c, "total_tokens", 0)
    before_cost = getattr(c, "total_cost", 0.0)
    results = await run_probes(c, probes, window)
    for r in results:
        r.ts = now_wall
        frame = r.to_dict()
        server.history.setdefault(r.probe_id, deque(maxlen=_HISTORY_CAP)).append(frame)
        await server.dash.broadcast(frame)
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
    else:
        # Partial failure used to be invisible: 3 cards update, 2 silently don't.
        # Name each probe that returned nothing this batch.
        returned = {r.probe_id for r in results}
        for p in probes:
            if p.id not in returned:
                await server.dash.broadcast(
                    {
                        "type": "error",
                        "message": (
                            f"Probe '{p.label or p.id}' failed this batch (see the server log)."
                        ),
                    }
                )
    # Cost frame for this analysed window: per-batch delta + running totals.
    batch_tok = getattr(c, "total_tokens", 0) - before_tok
    batch_cost = getattr(c, "total_cost", 0.0) - before_cost
    server.total_tokens += batch_tok
    server.total_cost += batch_cost
    await server.dash.broadcast(
        {
            "type": "cost",
            "tokens_total": server.total_tokens,
            "tokens_delta": batch_tok,
            "cost_total": round(server.total_cost, 6),
            "cost_delta": round(batch_cost, 6),
        }
    )
    await broadcast_stat(server, len(window))


def export_payload(server: LiveServer) -> dict:
    """Everything worth keeping from a session, as one JSON-able dict."""
    return {
        "exported_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "window": server.window.to_dict(),
        "model": server.llm_cfg.to_public_dict(),
        "totals": {
            "ingested": server.ingested,
            "tokens": server.total_tokens,
            "cost_usd": round(server.total_cost, 6),
        },
        "probes": [
            {"probe": p.to_dict(), "history": list(server.history.get(pid, ()))}
            for pid, p in server.probes.items()
        ],
    }


def export_csv(server: LiveServer) -> str:
    """Flatten the snapshot history to CSV: one row per (snapshot, category) or open text.

    Third-party chat content ends up in the ``text`` column — treat the file as
    untrusted data when importing into a spreadsheet (same caveat as the VOD .md).
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts_utc", "probe_id", "kind", "label", "n", "category", "pct", "text"])
    for pid, p in server.probes.items():
        for snap in server.history.get(pid, ()):
            ts = datetime.fromtimestamp(float(snap.get("ts") or 0.0), tz=UTC).isoformat(
                timespec="seconds"
            )
            base = [ts, pid, snap.get("kind", p.kind), snap.get("label", ""), snap.get("n", 0)]
            pct = snap.get("pct")
            if pct is not None:
                for cat, share in pct.items():
                    w.writerow([*base, cat, share, ""])
            else:
                w.writerow([*base, "", "", snap.get("text", "")])
    return buf.getvalue()


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
            # A user 'force_run' analyzes the current buffer immediately, bypassing the
            # pause state, the refresh timer, and the idle cost-guard.
            forced = server.force_now
            if forced:
                server.force_now = False
            # Otherwise emit a window only when it is due AND new messages arrived since the
            # last analysis — so an idle/quiet chat never triggers a paid LLM request.
            due = (
                not server.paused
                and server.ingested != last_analyzed
                and server.buffer.due(server.window, now)
            )
            # Spending cap: once cumulative cost reaches the budget, no analysis (auto
            # OR forced) launches. We tell the dashboard ONCE (budget_blocked latch) and
            # clear the latch as soon as we're back under budget (e.g. after a raise).
            over_budget = (
                server.llm_cfg.budget_usd > 0 and server.total_cost >= server.llm_cfg.budget_usd
            )
            if over_budget:
                if (forced or due) and not server.budget_blocked:
                    await server.dash.broadcast(
                        {
                            "type": "error",
                            "message": (
                                f"Budget ${server.llm_cfg.budget_usd:.2f} reached — analyses "
                                "paused. Raise the budget or reset."
                            ),
                        }
                    )
                    server.budget_blocked = True
            else:
                server.budget_blocked = False
            if (
                not server.processing
                and server.probes
                and len(server.buffer)
                and (forced or due)
                and not over_budget
            ):
                # Capture the cleaned sample synchronously (O(n), not O(capacity)),
                # then reset the windowing timers.
                window = server.buffer.sample(server.window)
                server.buffer.mark_emitted(now)
                last_analyzed = server.ingested
                server.processing = True
                # NON-BLOCKING: the LLM call runs in the background so the loop keeps
                # draining the queue and flushing the feed while it works.
                asyncio.create_task(_run_window(server, window, time.time()))
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
  if (window.__viewlyt_running) { console.warn("[viewlyt] already capturing on this page; ignoring a second injection."); return; }
  window.__viewlyt_running = true;
  var PORT = "%PORT%";
  // Safari blocks insecure ws:// from an https page (mixed content) EXCEPT to the
  // literal host "localhost" (127.0.0.1 is not exempt on older WebKit), so when the
  // server is on loopback we rotate between both spellings until one connects.
  var HOSTS = (function () { var h = "%HOST%"; if (h === "127.0.0.1") return [h, "localhost"]; if (h === "localhost") return [h, "127.0.0.1"]; return [h]; })();
  var hostIdx = 0, everConnected = false;
  function wsUrl() { return "ws://" + HOSTS[hostIdx % HOSTS.length] + ":" + PORT + "/ingest"; }
  var ws, captured = 0, connected = false, pinger, flusher, scrollTimer, lastCapMs = 0, outbox = [];
  var badge = document.createElement("div");
  badge.style.cssText = "position:fixed;z-index:2147483647;right:8px;bottom:8px;font:12px/1.4 system-ui,sans-serif;color:#fff;padding:6px 10px;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.45);max-width:70vw";
  function paint(text, color) { badge.textContent = "viewlyt: " + text; badge.style.background = color || "#1e3a8a"; }
  (document.body || document.documentElement).appendChild(badge);
  function status() { paint("connected | captured " + captured, "#14532d"); }
  function connect() {
    var url = wsUrl();
    try { ws = new WebSocket(url); } catch (e) {
      // Safari throws HERE (synchronously) when mixed content blocks the socket.
      if (!everConnected) hostIdx++;
      paint("browser blocked ws:// - see console", "#7f1d1d");
      console.warn("[viewlyt] new WebSocket(" + url + ") threw: " + e + " -- Safari blocks insecure ws:// from https pages except to 'localhost' (retrying with the next host), and content blockers (uBlock/AdGuard) can block local connections: allowlist youtube.com there.");
      setTimeout(connect, 2000);
      return;
    }
    ws.onopen = function () { connected = true; everConnected = true; console.log("[viewlyt] connected", url); status(); if (pinger) clearInterval(pinger); pinger = setInterval(function () { if (ws && ws.readyState === 1) ws.send('{"type":"ping"}'); }, 15000); };
    ws.onclose = function () { connected = false; if (pinger) clearInterval(pinger); if (!everConnected) hostIdx++; paint("disconnected, retrying...", "#78350f"); setTimeout(connect, 2000); };
    ws.onerror = function () { connected = false; paint("CANNOT REACH SERVER - see console", "#7f1d1d"); console.warn("[viewlyt] WebSocket to " + url + " failed. Usual causes: (1) Safari blocks insecure ws:// from https pages except to 'localhost' - both host spellings are retried automatically, but content blockers can also block local connections (allowlist youtube.com / disable for this site); (2) Chrome: accept the one-time 'local network' permission; (3) an ad blocker blocking 127.0.0.1 (uBlock Origin: turn off 'Block outsider intrusion into LAN', or allowlist this page). The separate 'ad_break ERR_BLOCKED_BY_CLIENT' line is just your ad blocker and does NOT affect viewlyt."); };
  }
  function send(obj) { outbox.push(obj); if (outbox.length > 3000) outbox.shift(); }
  function flush() { if (!outbox.length || !ws || ws.readyState !== 1) return; var n = outbox.length; ws.send(JSON.stringify(outbox.splice(0, n))); if (connected) status(); }
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
    { "matches": ["https://www.youtube.com/live_chat*"], "js": ["content.js"], "run_at": "document_idle" }
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

    @app.get("/export.json")
    async def export_json() -> Response:
        return Response(
            json.dumps(export_payload(server), ensure_ascii=False),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="viewlyt-live-export.json"'},
        )

    @app.get("/export.csv")
    async def export_csv_route() -> Response:
        return Response(
            export_csv(server),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="viewlyt-live-export.csv"'},
        )

    @app.get("/")
    async def index() -> HTMLResponse:
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return HTMLResponse(page.read_text(encoding="utf-8"))
        return HTMLResponse(_NOT_BUILT_HTML)

    @app.websocket("/ingest")
    async def ingest(ws: WebSocket) -> None:
        if not _origin_allowed(
            ws.headers.get("origin"),
            ws.url.hostname or "127.0.0.1",
            ws.url.port or 8000,
            allow_youtube=True,
        ):
            await ws.close(code=1008)
            return
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
        if not _origin_allowed(
            ws.headers.get("origin"),
            ws.url.hostname or "127.0.0.1",
            ws.url.port or 8000,
            allow_youtube=False,
        ):
            await ws.close(code=1008)
            return
        await server.dash.connect(ws)
        try:
            await ws.send_json(server.state_message())
            if server.history:
                # Backfill: a fresh/reloaded dashboard replays the stored snapshots
                # instead of starting blank.
                await ws.send_json(server.history_message())
            while True:
                await ws.receive_text()  # keepalive pings, ignored
        except (WebSocketDisconnect, Exception):
            pass
        finally:
            server.dash.disconnect(ws)

    @app.websocket("/control")
    async def control(ws: WebSocket) -> None:
        if not _origin_allowed(
            ws.headers.get("origin"),
            ws.url.hostname or "127.0.0.1",
            ws.url.port or 8000,
            allow_youtube=False,
        ):
            await ws.close(code=1008)
            return
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
    st = persistence.load_state()
    if st:
        server.window = WindowConfig.from_dict(st["window"])
        server.buffer = WindowBuffer(maxlen=server.window.capacity)
        m = st["model"]
        server.llm_cfg = LLMConfig(
            base_url=m["base_url"],
            api_key=m.get("api_key", ""),
            model=m["model"],
            budget_usd=float(m.get("budget", 0.0)),
            language=str(m.get("language") or "Portuguese (Brazil)"),
        )
        server._client = None
        for pd in st.get("probes", []):
            try:
                p = probe_from_dict(pd)
                server.probes[p.id] = p
            except Exception:
                logger.warning("skipping unrestorable saved probe: %r", pd, exc_info=True)
    else:
        # No saved state yet: seed the file with the current config.
        persistence.save_state(
            server.window.to_dict(),
            {
                "base_url": server.llm_cfg.base_url,
                "model": server.llm_cfg.model,
                "api_key": server.llm_cfg.api_key,
                "budget": server.llm_cfg.budget_usd,
                "language": server.llm_cfg.language,
            },
            [p.to_dict() for p in server.probes.values()],
        )
    app = create_app(server)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "binding to %s exposes the dashboard AND the stored LLM API key to the network "
            "with NO authentication — only do this on a trusted network.",
            host,
        )
    if open_browser:
        try:
            webbrowser.open("http://" + host + ":" + str(port) + "/")
        except Exception:
            pass
    uvicorn.run(app, host=host, port=port, log_level="warning")
