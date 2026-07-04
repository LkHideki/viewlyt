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
from itertools import islice

from .messages import ChatMessage, clean_chat

# Raw-tail margin per target message when sampling: enough for a heavy spam
# collapse (~75% of the tail folding away) without ever cleaning the whole buffer.
_CLEAN_MARGIN = 4


@dataclass(slots=True)
class WindowConfig:
    n: int = 230
    overlap: int = 0
    gap: float = 45.0  # seconds; time-based refresh interval (time / hybrid)
    mode: str = "hybrid"  # "count" | "time" | "hybrid"
    capacity: int = 3000  # max messages kept in the rolling sample buffer
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
            "capacity": self.capacity,
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
            capacity=max(100, int(d.get("capacity", cur.capacity))),
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

    def tail(self, k: int) -> list[ChatMessage]:
        """Return the last ``min(k, len)`` messages in order — O(k), not O(capacity)."""
        if k <= 0:
            return []
        if k >= len(self._buf):
            return list(self._buf)
        out = list(islice(reversed(self._buf), k))
        out.reverse()
        return out

    def _window(self, cfg: WindowConfig) -> list[ChatMessage]:
        return self.tail(max(1, cfg.n))

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

    def mark_emitted(self, now: float) -> None:
        """Reset the windowing counters/timers after a snapshot was taken."""
        self._since_last = 0
        self._last_emit = now

    def emit(self, cfg: WindowConfig, now: float) -> list[ChatMessage]:
        """Mark a snapshot taken and return its window of messages."""
        self.mark_emitted(now)
        return self._window(cfg)

    def sample(self, cfg: WindowConfig) -> list[ChatMessage]:
        """The cleaned analysis window: the last ``n`` messages after spam hygiene.

        Cleans only a bounded raw tail (``_CLEAN_MARGIN × n``) instead of the whole
        buffer, so the cost scales with the window size, not with ``capacity``.
        Under extreme spam (more than ~75% of the tail collapsing) the sample can
        come out shorter than ``n`` — the target is a target, not a guarantee.
        """
        n = max(1, cfg.n)
        raw = self.tail(_CLEAN_MARGIN * n)
        return clean_chat(raw, dedupe=cfg.dedupe, merge_authors=cfg.merge_authors)[-n:]

    def snapshot(self) -> list[ChatMessage]:
        """Return all buffered messages without mutating the buffer."""
        return list(self._buf)

    def offer(self, msg: ChatMessage, cfg: WindowConfig, now: float) -> list[ChatMessage] | None:
        """Add a message; return the window if this makes a snapshot due, else ``None``."""
        self.add(msg)
        if self.due(cfg, now):
            return self.emit(cfg, now)
        return None
