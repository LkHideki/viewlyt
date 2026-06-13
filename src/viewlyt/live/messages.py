"""Turn a raw ``/ingest`` payload into a normalized :class:`ChatMessage`.

Pure / stdlib-only. Reuses the project's HTML→text pipeline so a live-chat
message is cleaned the EXACT same way a VOD comment is: the emoji ``alt`` is
preserved (the browser snippet sends ``#message``'s ``innerHTML``) and the text
is flattened to a single line.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..htmltext import flatten_inline, html_to_text


@dataclass(slots=True)
class ChatMessage:
    author: str
    text: str
    ts: float  # epoch seconds
    id: str = ""


def _normalize_ts(value: object) -> float:
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    # The browser snippet sends Date.now() in milliseconds; fold to seconds.
    return ts / 1000.0 if ts > 1e11 else ts


def message_from_ingest(payload: dict) -> ChatMessage | None:
    """Build a :class:`ChatMessage` from an ingest payload, or ``None`` if empty.

    Prefers ``html`` (keeps emoji ``alt``); falls back to a plain ``text`` field.
    Messages whose text is empty after cleaning are dropped (returns ``None``), so
    membership/sticker rows with no text don't pollute the sample.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("html")
    if raw is None:
        raw = payload.get("text") or ""
    text = flatten_inline(html_to_text(str(raw)))
    if not text:
        return None
    author = str(payload.get("author") or "").strip() or "unknown"
    return ChatMessage(
        author=author,
        text=text,
        ts=_normalize_ts(payload.get("ts")),
        id=str(payload.get("id") or ""),
    )
