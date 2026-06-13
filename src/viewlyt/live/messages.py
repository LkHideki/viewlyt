"""Turn a raw ``/ingest`` payload into a normalized :class:`ChatMessage`.

Pure / stdlib-only. Reuses the project's HTML→text pipeline so a live-chat
message is cleaned the EXACT same way a VOD comment is: the emoji ``alt`` is
preserved (the browser snippet sends ``#message``'s ``innerHTML``) and the text
is flattened to a single line.
"""

from __future__ import annotations

import re
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


# --- spam hygiene -----------------------------------------------------------
# A user spamming a window with identical/near-identical lines (or one person
# splitting a thought across many messages) skews the sample. We collapse that
# the same way the VOD path does in htmltext.group_consecutive_comments: drop a
# user's near-duplicates, then concatenate their consecutive messages.

_ANON_AUTHORS = {"", "unknown"}
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_RUN_RE = re.compile(r"(.)\1{2,}")


def _norm(text: str) -> str:
    """Normalized key for near-duplicate ("semantic-lite") comparison.

    Casefolds, strips punctuation/emoji separators, caps long character runs
    ("loool" -> "lool") and collapses whitespace — enough to fold a user spamming
    the same line in slightly different forms without an embedding model.
    """
    t = _PUNCT_RE.sub("", text.casefold())
    t = _RUN_RE.sub(r"\1\1", t)
    return " ".join(t.split())


def drop_duplicates(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Drop spammy near-duplicates, keeping the first of each (author, normalized text).

    The key is per-author (like the VOD rule), so one user repeating a line
    collapses to one occurrence, while two different people reacting with the same
    word both survive. Emoji-only messages (empty normalized text) are always kept.
    Does not mutate the input.
    """
    seen: set[tuple[str, str]] = set()
    out: list[ChatMessage] = []
    for m in messages:
        norm = _norm(m.text)
        if norm and (m.author, norm) in seen:
            continue
        if norm:
            seen.add((m.author, norm))
        out.append(m)
    return out


def merge_consecutive(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Concatenate consecutive messages from the same real author into one.

    Keeps the first message's author/ts/id and joins texts in order with " / ".
    Anonymous authors ("" / "unknown") are never merged — two unknowns aren't the
    same person — matching the VOD merge. Does not mutate the input (the head of a
    run is copied before its text grows).
    """
    out: list[ChatMessage] = []
    for m in messages:
        prev = out[-1] if out else None
        if (
            prev is not None
            and m.author == prev.author
            and m.author.casefold() not in _ANON_AUTHORS
        ):
            prev.text = f"{prev.text} / {m.text}"
        else:
            out.append(ChatMessage(author=m.author, text=m.text, ts=m.ts, id=m.id))
    return out


def clean_chat(
    messages: list[ChatMessage], *, dedupe: bool = True, merge_authors: bool = True
) -> list[ChatMessage]:
    """Spam hygiene for a sampled window: drop near-duplicates, then merge author runs.

    The order matches the intent: first collapse a user's repeated/identical spam,
    then concatenate what remains from the same author so one person counts once.
    Either step can be disabled. Pure; does not mutate the input.
    """
    out = messages
    if dedupe:
        out = drop_duplicates(out)
    if merge_authors:
        out = merge_consecutive(out)
    return out
