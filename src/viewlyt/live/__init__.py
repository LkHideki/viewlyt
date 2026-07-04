"""viewlyt.live — real-time YouTube live-chat audience analysis with LLMs.

Opt-in subpackage (install ``viewlyt[live]``). The pure pieces — :mod:`messages`,
:mod:`window`, :mod:`probes` — are stdlib-only and import WITHOUT FastAPI/openai,
so ``import viewlyt`` stays lightweight and they unit-test without a browser or a
network. The FastAPI server (:mod:`server`) and the LLM client (:mod:`llm`) pull
their heavy deps in lazily, only when used.

Run it with ``vl live`` (or ``vl help live`` for every flag).
"""

from __future__ import annotations

from .messages import (
    ChatMessage,
    clean_chat,
    drop_duplicates,
    merge_consecutive,
    message_from_ingest,
)
from .probes import (
    PROBE_REGISTRY,
    ClassificationProbe,
    OpenSummaryProbe,
    Probe,
    ProbeResult,
    probe_from_dict,
)
from .window import WindowBuffer, WindowConfig

__all__ = [
    "ChatMessage",
    "message_from_ingest",
    "clean_chat",
    "drop_duplicates",
    "merge_consecutive",
    "WindowBuffer",
    "WindowConfig",
    "Probe",
    "ProbeResult",
    "ClassificationProbe",
    "OpenSummaryProbe",
    "PROBE_REGISTRY",
    "probe_from_dict",
]
