"""Pure rolling-window buffer over the incoming chat stream.

The worker feeds every message in; the buffer decides when a *snapshot* (a window
of messages) is handed to the LLMs. Default policy is count-based::

    stride = max(1, n - overlap)   # how many NEW messages trigger a snapshot
    window = the last min(n, len(buffer)) messages

``time`` mode fires every ``gap`` seconds; ``hybrid`` fires on whichever comes
first. Everything is deterministic given the ``now`` you pass in, so it
unit-tests without a real clock. No Selenium, no I/O.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .messages import ChatMessage


@dataclass(slots=True)
class WindowConfig:
    n: int = 80
    overlap: int = 20
    gap: float = 15.0  # seconds (time / hybrid)
    mode: str = "count"  # "count" | "time" | "hybrid"
    dedupe: bool = True  # drop a user's near-duplicate (spam) messages
    merge_authors: bool = True  # concatenate consecutive same-author messages

    @property
    def stride(self) -> int:
        return max(1, self.n - max(0, self.overlap))

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "overlap": self.overlap,
            "gap": self.gap,
            "mode": self.mode,
            "dedupe": self.dedupe,
            "merge_authors": self.merge_authors,
        }

    @classmethod
    def from_dict(cls, d: dict) -> WindowConfig:
        cur = cls()
        return cls(
            n=max(1, int(d.get("n", cur.n))),
            overlap=max(0, int(d.get("overlap", cur.overlap))),
            gap=max(0.0, float(d.get("gap", cur.gap))),
            mode=str(d.get("mode", cur.mode)),
            dedupe=bool(d.get("dedupe", cur.dedupe)),
            merge_authors=bool(d.get("merge_authors", cur.merge_authors)),
        )


class WindowBuffer:
    """Holds recent messages and emits windows per the active :class:`WindowConfig`."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._buf: deque[ChatMessage] = deque(maxlen=maxlen)
        self._since_last = 0
        self._last_emit: float | None = None

    def __len__(self) -> int:
        return len(self._buf)

    def add(self, msg: ChatMessage) -> None:
        self._buf.append(msg)
        self._since_last += 1

    def _window(self, cfg: WindowConfig) -> list[ChatMessage]:
        return list(self._buf)[-max(1, cfg.n) :]

    def due(self, cfg: WindowConfig, now: float) -> bool:
        """Should a snapshot be emitted right now?"""
        if not self._buf:
            return False
        count_due = self._since_last >= cfg.stride
        time_due = self._last_emit is None or (now - self._last_emit) >= cfg.gap
        if cfg.mode == "time":
            return time_due
        if cfg.mode == "hybrid":
            return count_due or time_due
        return count_due  # "count" (default)

    def emit(self, cfg: WindowConfig, now: float) -> list[ChatMessage]:
        """Mark a snapshot taken and return its window of messages."""
        self._since_last = 0
        self._last_emit = now
        return self._window(cfg)

    def offer(self, msg: ChatMessage, cfg: WindowConfig, now: float) -> list[ChatMessage] | None:
        """Add a message; return the window if this makes a snapshot due, else ``None``."""
        self.add(msg)
        if self.due(cfg, now):
            return self.emit(cfg, now)
        return None
