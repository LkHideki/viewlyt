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
from .probes import Probe, ProbeResult, _numbered

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
    budget_usd: float = 0.0  # spending cap in USD; 0 = off (no cap)

    def to_public_dict(self) -> dict:
        # Never leak the api_key to the dashboard.
        return {"base_url": self.base_url, "model": self.model, "budget": self.budget_usd}


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
        self.total_tokens = 0
        self.total_cost = 0.0
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
            # Ask OpenRouter to include generation usage (token counts + USD cost) in
            # the response, so _record_usage can accumulate real spend.
            self._usage_extra: dict = {"usage": {"include": True}}
        else:
            self._usage_extra = {}
        self._client = AsyncOpenAI(**kwargs)

    def _record_usage(self, resp: object) -> None:
        """Accumulate token/cost usage from a completion response (never raises).

        ``total_tokens`` falls back to ``prompt + completion`` when absent. ``cost``
        comes from the provider (OpenRouter exposes USD on ``usage.cost``; on the
        pydantic model it lands in ``model_extra``) and is ``0`` when not provided.
        """
        try:
            u = getattr(resp, "usage", None)
            if u is None:
                return
            tokens = getattr(u, "total_tokens", 0) or (
                getattr(u, "prompt_tokens", 0) + getattr(u, "completion_tokens", 0)
            )
            cost = getattr(u, "cost", None)
            if cost is None:
                cost = (getattr(u, "model_extra", None) or {}).get("cost")
            self.total_tokens += int(tokens or 0)
            self.total_cost += float(cost or 0.0)
        except Exception:
            pass

    async def run(self, probe: Probe, messages: list[ChatMessage]) -> dict:
        system, user = probe.build_prompt(messages)
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        schema = probe.output_schema()
        # Structured-output fallback chain: try json_schema, then json_object; if both
        # fail (provider doesn't support them) fall through to a plain call.
        extra = {"extra_body": self._usage_extra} if self._usage_extra else {}
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
                    **extra,
                )
                self._record_usage(resp)
                return parse_json_loose(resp.choices[0].message.content or "")
            except Exception:
                continue
        # Plain call — intentionally unguarded so a real failure (endpoint down /
        # bad key / network error) propagates up to run_probes, which surfaces it
        # in the dashboard error banner rather than silently returning {}.
        resp = await self._client.chat.completions.create(
            model=self.model, messages=msgs, temperature=0, **extra
        )
        self._record_usage(resp)
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
        extra = {"extra_body": self._usage_extra} if self._usage_extra else {}
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
                    **extra,
                )
                self._record_usage(resp)
                return parse_json_loose(resp.choices[0].message.content or "")
            except Exception:
                continue
        resp = await self._client.chat.completions.create(
            model=self.model, messages=msgs, temperature=0, **extra
        )
        self._record_usage(resp)
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
    * ``kind == "auto"`` → the model FIRST decides between the two kinds, so the
      returned dict ALSO carries a ``"kind"`` key (``"open"`` or ``"classification"``)
      alongside that kind's normal fields. The explicit kinds never return ``"kind"``.
    """
    caller_cats = _clean_categories(categories)
    system = (
        "You are configuring an analysis probe that runs over a SAMPLE of MANY "
        "YouTube live-chat messages at once (the whole 'mass' of chat), NEVER a "
        "single message. Rewrite the user's quick request into a precise probe "
        "spec that analyzes the WHOLE sample together, grounded in what the "
        "messages actually say. Answer ONLY with the required JSON."
    )

    if kind == "auto":
        return await _rewrite_auto(client, system, text, caller_cats)

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


async def _rewrite_auto(client: LLMRunner, system: str, text: str, caller_cats: list[str]) -> dict:
    """Fully automatic rewrite: the model picks the kind AND returns its spec.

    Unlike the explicit paths, the returned dict CARRIES a ``"kind"`` key. A single
    permissive ``json_schema`` lets the model fill in either the open fields
    (``instruction``/``max_words``) or the classification fields
    (``question``/``categories``/``chart``); we then normalize defensively for the
    kind it chose, reusing the same fallbacks as the explicit paths.
    """
    schema = {
        "name": "auto_probe_spec",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["open", "classification"]},
                "label": {"type": "string"},
                "instruction": {"type": "string"},
                "max_words": {"type": "integer"},
                "question": {"type": "string"},
                "categories": {"type": "array", "items": {"type": "string"}},
                "chart": {"type": "string"},
            },
            "required": ["kind", "label"],
        },
    }
    if caller_cats:
        cats_line = (
            "If you choose 'classification', the user already picked these categories "
            f"and you MUST keep them EXACTLY: {', '.join(caller_cats)}.\n"
        )
    else:
        cats_line = (
            "If you choose 'classification', pick 3-6 mutually-exclusive, lowercase, "
            "collectively-exhaustive categories.\n"
        )
    user = (
        f"User request: {text}\n\n"
        "FIRST decide the best probe 'kind' for this request over the whole sample:\n"
        "- 'classification' sorts EACH message into 3-6 mutually-exclusive categories "
        "to produce live percentages (sentiment, yes/no, topic buckets);\n"
        "- 'open' writes a short synthesis across all messages (e.g. 'list the top 3 "
        "complaints', 'what are people asking about').\n"
        "THEN return the full spec for the kind you chose.\n"
        "For 'open': set 'instruction' (start it like \"Across all the sampled "
        "live-chat messages, ...\") and 'max_words' (caps the answer length).\n"
        f"For 'classification': set 'question' (how to classify each message), "
        f"'categories', and 'chart' (one of {', '.join(CHART_TYPES)}). {cats_line}"
        'The "label" is always a short 2-4 word title.\n'
        'Return e.g. {"kind":"open","instruction":"...","label":"...","max_words":60} '
        'or {"kind":"classification","question":"...","categories":["...","..."],'
        '"label":"...","chart":"bars"}.'
    )
    try:
        spec = await client.complete_json(system, user, schema)
    except Exception:
        spec = {}
    spec = spec if isinstance(spec, dict) else {}

    chosen = str(spec.get("kind") or "").strip().lower()
    label = str(spec.get("label") or "").strip()
    if chosen == "classification":
        question = str(spec.get("question") or "").strip() or text
        cats = _clean_categories(spec.get("categories"))
        if caller_cats:
            cats = caller_cats
        elif len(cats) < 2:
            cats = ["positive", "negative", "neutral"]
        chart = str(spec.get("chart") or "").strip().lower()
        if chart not in CHART_TYPES:
            chart = "bars"
        return {
            "kind": "classification",
            "question": question,
            "categories": cats,
            "label": label,
            "chart": chart,
        }
    # Default (and the explicit 'open' choice) → open synthesis.
    instruction = str(spec.get("instruction") or "").strip() or text
    max_words = _clamp_int(spec.get("max_words"), 60, 10, 200)
    return {
        "kind": "open",
        "instruction": instruction,
        "max_words": max_words,
        "label": label,
    }


def _normalize_suggested_probe(raw: object) -> dict:
    """Normalize ONE suggested probe (a raw dict from the model) into a full spec.

    Mirrors the per-kind normalization of :func:`rewrite_probe_spec`/:func:`_rewrite_auto`
    EXACTLY: an ``open`` spec yields ``{kind, instruction, max_words, label}`` and a
    ``classification`` spec yields ``{kind, question, categories(3-6), label, chart}``
    (chart forced into :data:`CHART_TYPES`, ≥2 categories guaranteed). Never raises:
    a non-dict / missing kind defaults to a usable ``open`` spec.
    """
    spec = raw if isinstance(raw, dict) else {}
    label = str(spec.get("label") or "").strip()
    chosen = str(spec.get("kind") or "").strip().lower()
    if chosen == "classification":
        question = str(spec.get("question") or "").strip()
        cats = _clean_categories(spec.get("categories"))
        if len(cats) < 2:
            cats = ["positive", "negative", "neutral"]
        chart = str(spec.get("chart") or "").strip().lower()
        if chart not in CHART_TYPES:
            chart = "bars"
        return {
            "kind": "classification",
            "question": question,
            "categories": cats,
            "label": label,
            "chart": chart,
        }
    # Default (and the explicit 'open' choice) → open synthesis.
    instruction = str(spec.get("instruction") or "").strip()
    max_words = _clamp_int(spec.get("max_words"), 60, 10, 200)
    return {
        "kind": "open",
        "instruction": instruction,
        "max_words": max_words,
        "label": label,
    }


async def suggest_probes(client: LLMRunner, text: str, messages: list[ChatMessage]) -> list[dict]:
    """Propose EXACTLY TWO probes worth running right now over a SAMPLE of chat.

    A meta-prompt that, given the user's (possibly empty) request AND the chat
    sample, proposes two distinct, direct, cohesive probes (a mix of ``open``
    synthesis and categorical ``classification``, whichever fits each). Each
    returned dict is a full probe spec (carrying its ``kind``) normalized the same
    way as :func:`rewrite_probe_spec`. Defensive: NEVER raises — returns ``[]`` on
    total failure (and may return fewer than two if the model returns junk).
    """
    sample = _numbered(messages) if messages else "(the chat sample is empty)"
    request = text.strip() or "(no specific request — propose what is most useful)"
    system = (
        "You design analysis probes that each run over a SAMPLE of MANY YouTube "
        "live-chat messages at once (the whole 'mass' of chat), NEVER a single "
        "message. Given the user's request (which may be empty) and the chat "
        "sample, propose EXACTLY TWO distinct, direct, cohesive probes most worth "
        "running right now. Each probe is either 'open' (a short synthesis across "
        "all messages) or 'classification' (sort EACH message into 3-6 mutually-"
        "exclusive categories for live percentages) — pick whichever fits each. "
        "Make the two probes complement each other. Answer ONLY with the required JSON."
    )
    user = (
        f"User request: {request}\n\n"
        f"Chat sample:\n{sample}\n\n"
        "Propose the two best probes. For an 'open' probe set 'instruction' (start "
        "it like \"Across all the sampled live-chat messages, ...\") and 'max_words'. "
        "For a 'classification' probe set 'question' (how to classify each message), "
        f"'categories' (3-6, lowercase, mutually exclusive), and 'chart' (one of "
        f"{', '.join(CHART_TYPES)}). Every probe needs a short 2-4 word 'label' and "
        "its 'kind'. Return {\"probes\":[{...},{...}]} with EXACTLY two entries."
    )
    schema = {
        "name": "suggested_probes",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "probes": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "kind": {"type": "string", "enum": ["open", "classification"]},
                            "label": {"type": "string"},
                            "instruction": {"type": "string"},
                            "max_words": {"type": "integer"},
                            "question": {"type": "string"},
                            "categories": {"type": "array", "items": {"type": "string"}},
                            "chart": {"type": "string"},
                        },
                        "required": ["kind", "label"],
                    },
                }
            },
            "required": ["probes"],
        },
    }
    try:
        parsed = await client.complete_json(system, user, schema)
    except Exception:
        return []
    parsed = parsed if isinstance(parsed, dict) else {}
    raw_probes = parsed.get("probes")
    if not isinstance(raw_probes, list):
        return []
    return [_normalize_suggested_probe(p) for p in raw_probes[:2]]
