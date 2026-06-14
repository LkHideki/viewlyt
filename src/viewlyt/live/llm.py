"""Unified OpenAI-compatible LLM client (cloud OR local).

One client speaks to OpenAI cloud, **LM Studio** (``localhost:1234/v1``), Ollama,
or vLLM — only ``base_url``/``api_key``/``model`` change. ``openai`` is imported
lazily inside the constructor, so the pure modules (and ``import viewlyt``) never
pull it in.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol

from .messages import ChatMessage
from .probes import Probe, ProbeResult

logger = logging.getLogger("viewlyt.live")

_DEFAULT_BASE_URL = "http://localhost:1234/v1"  # LM Studio's default local server

PROVIDERS: dict[str, str] = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}


def provider_base_url(provider: str) -> str:
    """Return the canonical base_url for a provider key, or _DEFAULT_BASE_URL."""
    return PROVIDERS.get(provider, _DEFAULT_BASE_URL)


@dataclass(slots=True)
class LLMConfig:
    base_url: str = "https://openrouter.ai/api/v1"
    api_key: str = ""  # empty by default; provide via --api-key or OPENROUTER_API_KEY env var
    model: str = "google/gemini-3.1-flash-lite"  # OpenRouter model (or override with --model)
    timeout: float = 60.0

    def to_public_dict(self) -> dict:
        # Never leak the api_key to the dashboard.
        return {"base_url": self.base_url, "model": self.model}


def parse_json_loose(content: str) -> dict:
    """Best-effort JSON parse: direct, else first ``{...}``/``[...]`` block, else ``{}``.

    Local models don't always honor ``strict`` json_schema, so we stay forgiving.
    A bare top-level array is wrapped as ``{"labels": [...]}`` (the classification shape).
    """
    if not content:
        return {}
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list):
            return {"labels": obj}
        return {}
    except Exception:
        pass
    m = re.search(r"\{.*\}|\[.*\]", content, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {"labels": obj}
        except Exception:
            return {}
    return {}


class LLMRunner(Protocol):
    """What the server/worker needs from a client (real or fake)."""

    model: str

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict: ...

    async def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


class LLMClient:
    """Calls an OpenAI-compatible chat endpoint and returns the parsed JSON dict."""

    def __init__(self, cfg: LLMConfig) -> None:
        from openai import AsyncOpenAI  # lazy: keeps the pure path openai-free

        self.cfg = cfg
        self.model = cfg.model
        kwargs: dict = {
            "base_url": cfg.base_url,
            "api_key": cfg.api_key or "x",
            "timeout": cfg.timeout,
        }
        if "openrouter" in cfg.base_url:
            # OpenRouter's optional headers for model-usage ranking on their leaderboard.
            kwargs["default_headers"] = {
                "HTTP-Referer": "https://github.com/LkHideki/viewlyt",
                "X-Title": "viewlyt",
            }
        self._client = AsyncOpenAI(**kwargs)

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        system, user = probe.build_prompt(messages)
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        schema = probe.output_schema()
        # Structured-output fallback chain: try json_schema, then json_object; if both
        # fail (provider doesn't support them) fall through to a plain call.
        for rf in (
            {"type": "json_schema", "json_schema": schema},
            {"type": "json_object"},
        ):
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    temperature=0,
                    response_format=rf,
                )
                return parse_json_loose(resp.choices[0].message.content or "")
            except Exception:
                continue
        # Plain call — intentionally unguarded so a real failure (endpoint down /
        # bad key / network error) propagates up to run_probes, which surfaces it
        # in the dashboard error banner rather than silently returning {}.
        resp = await self._client.chat.completions.create(
            model=self.model, messages=msgs, temperature=0
        )
        return parse_json_loose(resp.choices[0].message.content or "")

    async def complete_json(self, system: str, user: str, schema: dict) -> dict:
        """One-shot structured completion: ``(system, user)`` → parsed JSON dict.

        Mirrors :meth:`run`'s structured-output fallback chain exactly — try
        ``json_schema``, then ``json_object``, then a plain call — so it works on
        providers that don't support structured outputs. The plain call is left
        unguarded so a real failure (endpoint/key/network) propagates to the caller.
        """
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        for rf in (
            {"type": "json_schema", "json_schema": schema},
            {"type": "json_object"},
        ):
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=msgs,
                    temperature=0,
                    response_format=rf,
                )
                return parse_json_loose(resp.choices[0].message.content or "")
            except Exception:
                continue
        resp = await self._client.chat.completions.create(
            model=self.model, messages=msgs, temperature=0
        )
        return parse_json_loose(resp.choices[0].message.content or "")


async def run_probes(
    client: LLMRunner, probes: list[Probe], messages: list[ChatMessage]
) -> list[ProbeResult]:
    """Run every probe over the same window; one failing probe never kills the rest.

    Pure orchestration over the (real or fake) ``client`` — no FastAPI here, so it
    unit-tests with a stub client and ``asyncio.run``. Returns one aggregated
    :class:`ProbeResult` per probe that succeeded, in order.
    """
    results: list[ProbeResult] = []
    for probe in probes:
        try:
            parsed = await client.run(probe, messages)
            results.append(probe.aggregate(parsed, messages))
        except Exception:
            logger.exception("probe %r failed", getattr(probe, "id", "?"))
    return results


# Chart visualizations a classification probe may request, in the dashboard's order.
CHART_TYPES: tuple[str, ...] = (
    "bars",
    "columns",
    "stacked",
    "donut",
    "lines",
    "area",
    "delta",
)


def _clamp_int(value: object, default: int, lo: int, hi: int) -> int:
    """Coerce ``value`` to int (default on failure) and clamp into ``[lo, hi]``."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _clean_categories(raw: object) -> list[str]:
    """Lowercase/strip a list-ish of category labels, dropping blanks and dupes."""
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = []
    for item in items:
        cat = str(item).strip().lower()
        if cat and cat not in seen:
            seen.add(cat)
            out.append(cat)
    return out


async def rewrite_probe_spec(
    client: LLMRunner, kind: str, text: str, categories: list[str] | None
) -> dict:
    """Rewrite a casual ask-bar request into a precise probe spec for a SAMPLE of chat.

    The probe configured here runs over MANY YouTube live-chat messages at once
    (the "mass"), never a single message. We ask the model to ground the spec in
    what the messages actually say and to answer ONLY with the required JSON, then
    defensively normalize every field (coercing types, never raising ``KeyError``).

    * ``kind == "open"`` → ``{"instruction", "label", "max_words"}``; the instruction
      directs the analyst to synthesize ACROSS ALL sampled messages.
    * ``kind == "classification"`` → ``{"question", "categories", "label", "chart"}``;
      3-6 mutually-exclusive lowercase categories. Caller-supplied categories always
      win (forced after the call).
    """
    caller_cats = _clean_categories(categories)
    system = (
        "You are configuring an analysis probe that runs over a SAMPLE of MANY "
        "YouTube live-chat messages at once (the whole 'mass' of chat), NEVER a "
        "single message. Rewrite the user's quick request into a precise probe "
        "spec that analyzes the WHOLE sample together, grounded in what the "
        "messages actually say. Answer ONLY with the required JSON."
    )

    if kind == "open":
        schema = {
            "name": "open_probe_spec",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "instruction": {"type": "string"},
                    "label": {"type": "string"},
                    "max_words": {"type": "integer"},
                },
                "required": ["instruction", "label"],
            },
        }
        user = (
            f"User request: {text}\n\n"
            "Produce an open-ended analysis spec. The 'instruction' MUST direct the "
            "analyst to synthesize ACROSS ALL the sampled live-chat messages (start "
            'it like "Across all the sampled live-chat messages, ..."). The "label" '
            "is a short 2-4 word title. 'max_words' caps the answer length.\n"
            'Return {"instruction":"...","label":"...","max_words":60}.'
        )
        try:
            spec = await client.complete_json(system, user, schema)
        except Exception:
            spec = {}
        spec = spec if isinstance(spec, dict) else {}
        instruction = str(spec.get("instruction") or "").strip() or text
        label = str(spec.get("label") or "").strip()
        max_words = _clamp_int(spec.get("max_words"), 60, 10, 200)
        return {"instruction": instruction, "label": label, "max_words": max_words}

    # kind == "classification" (and any unknown kind falls through to here)
    schema = {
        "name": "classification_probe_spec",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question": {"type": "string"},
                "categories": {"type": "array", "items": {"type": "string"}},
                "label": {"type": "string"},
                "chart": {"type": "string"},
            },
            "required": ["question", "categories", "label"],
        },
    }
    if caller_cats:
        cats_line = (
            "The user already chose these categories; you MUST keep them EXACTLY: "
            f"{', '.join(caller_cats)}.\n"
        )
    else:
        cats_line = (
            "Choose 3-6 mutually-exclusive, lowercase, collectively-exhaustive categories.\n"
        )
    user = (
        f"User request: {text}\n\n"
        "Produce a classification spec that sorts EACH message into one category. "
        "Rephrase the request as a clear 'question' to classify each message. "
        f"{cats_line}"
        'The "label" is a short 2-4 word title. "chart" is one of '
        f"{', '.join(CHART_TYPES)} (default 'bars').\n"
        'Return {"question":"...","categories":["...","..."],"label":"...","chart":"bars"}.'
    )
    try:
        spec = await client.complete_json(system, user, schema)
    except Exception:
        spec = {}
    spec = spec if isinstance(spec, dict) else {}

    question = str(spec.get("question") or "").strip() or text
    label = str(spec.get("label") or "").strip()
    cats = _clean_categories(spec.get("categories"))
    if caller_cats:
        # Caller wins: force the spec back to the cleaned caller categories.
        cats = caller_cats
    elif len(cats) < 2:
        cats = ["positive", "negative", "neutral"]
    chart = str(spec.get("chart") or "").strip().lower()
    if chart not in CHART_TYPES:
        chart = "bars"
    return {"question": question, "categories": cats, "label": label, "chart": chart}
