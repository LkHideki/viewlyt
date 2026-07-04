"""Dashboard broadcast fan-out: serialize once, send concurrently, drop bad sockets.

Uses fake WebSocket objects (only ``send_text`` is exercised) so no real server or
network is involved. Skipped when the optional FastAPI dep (``viewlyt[live]``) is
absent.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

pytest.importorskip("fastapi")

from viewlyt.live import server as live_server  # noqa: E402
from viewlyt.live.server import ConnectionManager  # noqa: E402


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)


class BrokenWS(FakeWS):
    async def send_text(self, payload: str) -> None:
        raise RuntimeError("send failed")


class StuckWS(FakeWS):
    async def send_text(self, payload: str) -> None:
        await asyncio.sleep(60)


def test_broadcast_sends_the_same_payload_to_all() -> None:
    mgr = ConnectionManager()
    a, b = FakeWS(), FakeWS()
    mgr.active = {a, b}  # type: ignore[assignment]
    asyncio.run(mgr.broadcast({"type": "stat", "ingested": 7}))
    assert json.loads(a.sent[0]) == {"type": "stat", "ingested": 7}
    assert a.sent == b.sent


def test_broadcast_drops_a_broken_socket_and_keeps_the_rest() -> None:
    mgr = ConnectionManager()
    ok, broken = FakeWS(), BrokenWS()
    mgr.active = {ok, broken}  # type: ignore[assignment]
    asyncio.run(mgr.broadcast({"t": 1}))
    assert broken not in mgr.active
    assert ok in mgr.active
    assert len(ok.sent) == 1


def test_broadcast_times_out_a_stuck_socket_without_stalling(monkeypatch) -> None:
    # One hung tab must not delay the healthy one (concurrent sends) nor block the
    # caller for its full sleep (timeout kicks in and the socket is dropped).
    monkeypatch.setattr(live_server, "_SEND_TIMEOUT", 0.05)
    mgr = ConnectionManager()
    ok, stuck = FakeWS(), StuckWS()
    mgr.active = {ok, stuck}  # type: ignore[assignment]
    t0 = time.monotonic()
    asyncio.run(mgr.broadcast({"t": 1}))
    assert time.monotonic() - t0 < 1.0
    assert stuck not in mgr.active
    assert ok in mgr.active
    assert len(ok.sent) == 1
