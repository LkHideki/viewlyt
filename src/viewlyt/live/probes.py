"""Pluggable 'probes': a question asked of a window of chat messages.

A probe knows how to (1) build the LLM prompt, (2) declare its JSON output schema,
and (3) aggregate the model's parsed answer into a :class:`ProbeResult`. Two kinds
ship:

* :class:`ClassificationProbe` — sort each message into one of N categories →
  percentages (with optional EMA smoothing across snapshots). Powers "how is the
  crowd feeling? happy / angry / neutral" with live-updating %.
* :class:`OpenSummaryProbe` — a free-form instruction → a short synthesized text
  ("what are the main complaints right now?").

A new kind registers in :data:`PROBE_REGISTRY` and then flows through the
server/dashboard for free. Probes are JSON-serializable, so the control screen
creates/edits them live. Pure: no Selenium, no openai, no I/O.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .messages import ChatMessage


@dataclass(slots=True)
class ProbeResult:
    probe_id: str
    kind: str
    label: str
    n: int
    ts: float = 0.0
    pct: dict[str, float] | None = None
    text: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "type": "result",
            "probe_id": self.probe_id,
            "kind": self.kind,
            "label": self.label,
            "n": self.n,
            "ts": self.ts,
        }
        if self.pct is not None:
            d["pct"] = self.pct
        if self.text is not None:
            d["text"] = self.text
        return d


def _auto_label(text: str) -> str:
    """Derive a short human label from a prompt string.

    Takes the first non-blank line, strips leading question openers (PT/EN),
    collapses whitespace, caps at 8 words and 48 chars, removes trailing
    punctuation. Returns 'probe' if nothing usable remains.
    """
    import re

    # 1. Take first non-blank line
    lines = text.split("\n")
    first_line = ""
    for line in lines:
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break
    if not first_line:
        return "probe"

    # 2. Strip trailing punctuation
    text_clean = first_line.rstrip("?:.,;!")

    # 3. Remove leading filler/question openers (PT and EN), case-insensitive
    patterns = [
        r"^(quais|qual)\s+(s[ãa]o\s+|[ée]\s+)?(os|as|o|a)\s+",
        r"^como\s+(est[ãa]o?|anda[m]?)\s+(os|as|o|a)\s+",
        r"^o que\s+",
        r"^(me\s+diga|liste|gere)\s+",
        r"^(what|which)\s+(are|is)\s+the\s+",
        r"^how\s+(are|is)\s+the\s+",
        r"^(summari[sz]e|list|tell\s+me)\s+(the\s+)?",
    ]
    for pattern in patterns:
        text_clean = re.sub(pattern, "", text_clean, count=1, flags=re.IGNORECASE)
        if text_clean != first_line.rstrip("?:.,;!"):
            break

    # 4. Collapse internal whitespace
    text_clean = " ".join(text_clean.split()).strip()

    # 5. Cap to 8 words and 48 chars, trim trailing partial word and punctuation
    words = text_clean.split()[:8]
    label = " ".join(words)
    if len(label) > 48:
        label = label[:48].rsplit(" ", 1)[0].rstrip("?:.,;!")
    else:
        label = label.rstrip("?:.,;!")

    # 6. Capitalize first character
    label = label.strip()
    if label:
        label = label[0].upper() + label[1:]
    return label or "probe"


def _numbered(messages: list[ChatMessage]) -> str:
    return "\n".join(f"{i}. {m.text}" for i, m in enumerate(messages, 1))


class Probe:
    """Base probe. Subclasses set ``kind`` and implement the three hooks."""

    kind = "base"

    def __init__(self, id: str, label: str) -> None:
        self.id = id
        self.label = label

    # --- hooks -----------------------------------------------------------
    def build_prompt(self, messages: list[ChatMessage]) -> tuple[str, str]:
        """Return ``(system, user)`` prompt strings for this window."""
        raise NotImplementedError

    def output_schema(self) -> dict:
        """Return the OpenAI ``json_schema`` object (``{name, strict, schema}``)."""
        raise NotImplementedError

    def aggregate(self, parsed: dict, messages: list[ChatMessage]) -> ProbeResult:
        """Fold the model's parsed JSON into a :class:`ProbeResult`."""
        raise NotImplementedError

    # --- (de)serialization ----------------------------------------------
    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict) -> Probe:
        return cls(id=str(d["id"]), label=str(d.get("label") or d["id"]))


class ClassificationProbe(Probe):
    kind = "classification"

    def __init__(
        self,
        id: str,
        label: str,
        question: str,
        categories: list[str],
        ema_alpha: float = 0.0,
        chart: str = "bars",
    ) -> None:
        super().__init__(id, label)
        self.question = question
        self.categories = [c.strip() for c in categories if c.strip()]
        self.ema_alpha = float(ema_alpha)
        self.chart = str(chart) if chart else "bars"
        self._ema: dict[str, float] | None = None

    def build_prompt(self, messages: list[ChatMessage]) -> tuple[str, str]:
        cats = ", ".join(self.categories)
        system = (
            "You are a precise text classifier for YouTube live-chat messages. "
            f"Classify each numbered message into EXACTLY ONE of: {cats}. "
            "Answer ONLY with the required JSON. Do not explain."
        )
        user = (
            f"Question: {self.question}\n"
            f"Categories: {cats}\n\n"
            f"Messages:\n{_numbered(messages)}\n\n"
            'Return {"labels":[{"i":<message number>,"label":<category>}, ...]} '
            "with one entry per message."
        )
        return system, user

    def output_schema(self) -> dict:
        return {
            "name": "classification",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "i": {"type": "integer"},
                                "label": {"type": "string", "enum": self.categories},
                            },
                            "required": ["i", "label"],
                        },
                    }
                },
                "required": ["labels"],
            },
        }

    def aggregate(self, parsed: dict, messages: list[ChatMessage]) -> ProbeResult:
        counts: Counter[str] = Counter()
        for item in (parsed or {}).get("labels", []) or []:
            label = str(item.get("label", "")).strip()
            if label in self.categories:
                counts[label] += 1
        total = sum(counts.values())
        if total:
            pct = {c: round(100.0 * counts.get(c, 0) / total, 1) for c in self.categories}
        else:
            pct = {c: 0.0 for c in self.categories}
        if 0.0 < self.ema_alpha <= 1.0:
            pct = self._smooth(pct)
        return ProbeResult(
            probe_id=self.id,
            kind=self.kind,
            label=self.label,
            n=len(messages),
            pct=pct,
        )

    def _smooth(self, pct: dict[str, float]) -> dict[str, float]:
        a = self.ema_alpha
        if self._ema is None:
            self._ema = dict(pct)
        else:
            self._ema = {c: round(a * pct[c] + (1 - a) * self._ema.get(c, pct[c]), 1) for c in pct}
        return dict(self._ema)

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            question=self.question,
            categories=self.categories,
            ema_alpha=self.ema_alpha,
            chart=self.chart,
        )
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ClassificationProbe:
        question = str(d.get("question") or "")
        label = str(d.get("label") or "").strip()
        if not label:
            if question:
                label = _auto_label(question)
            else:
                categories = list(d.get("categories") or [])
                label = _auto_label(", ".join(categories)) if categories else str(d["id"])
        return cls(
            id=str(d["id"]),
            label=label,
            question=question,
            categories=list(d.get("categories") or []),
            ema_alpha=float(d.get("ema_alpha") or 0.0),
            chart=str(d.get("chart") or "bars"),
        )


class OpenSummaryProbe(Probe):
    kind = "open"

    def __init__(self, id: str, label: str, instruction: str, max_words: int = 60) -> None:
        super().__init__(id, label)
        self.instruction = instruction
        self.max_words = int(max_words)

    def build_prompt(self, messages: list[ChatMessage]) -> tuple[str, str]:
        system = (
            "You analyze a sample of YouTube live-chat messages and answer the "
            "user's question with a short, concrete synthesis grounded in what the "
            "messages actually say. Answer ONLY with the required JSON."
        )
        user = (
            f"Task: {self.instruction}\n"
            f"Answer in at most {self.max_words} words.\n\n"
            f"Messages:\n{_numbered(messages)}\n\n"
            'Return {"summary":"<your answer>"}.'
        )
        return system, user

    def output_schema(self) -> dict:
        return {
            "name": "summary",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        }

    def aggregate(self, parsed: dict, messages: list[ChatMessage]) -> ProbeResult:
        text = str((parsed or {}).get("summary", "")).strip()
        return ProbeResult(
            probe_id=self.id,
            kind=self.kind,
            label=self.label,
            n=len(messages),
            text=text,
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(instruction=self.instruction, max_words=self.max_words)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> OpenSummaryProbe:
        instruction = str(d.get("instruction") or "")
        label = str(d.get("label") or "").strip()
        if not label:
            label = _auto_label(instruction) if instruction else str(d["id"])
        return cls(
            id=str(d["id"]),
            label=label,
            instruction=instruction,
            max_words=int(d.get("max_words") or 60),
        )


PROBE_REGISTRY: dict[str, type[Probe]] = {
    ClassificationProbe.kind: ClassificationProbe,
    OpenSummaryProbe.kind: OpenSummaryProbe,
}


def probe_from_dict(d: dict) -> Probe:
    """Reconstruct a probe from its serialized form (dashboard → server)."""
    kind = str(d.get("kind") or "")
    try:
        klass = PROBE_REGISTRY[kind]
    except KeyError:
        raise ValueError(f"unknown probe kind: {kind!r}") from None
    return klass.from_dict(d)
