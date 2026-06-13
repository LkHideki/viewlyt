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
