"""Token counting and budget-aware splitting for the collected ``out/*.md``.

Two layers, mirroring the rest of the package:

* **Pure, dependency-free** — ``fmt_kt`` and ``split_by_budget`` (the splitter
  takes an injectable ``count`` callable, so it is fully testable without any
  tokenizer installed).
* **Lazy, opt-in** — the real token count uses `tiktoken` (extra ``tokens``),
  imported only when first needed (like how ``rag``/``live`` defer their heavy
  deps).

The default encoding is ``o200k_base`` (GPT-4o / o-series). Every LLM tokenizes
a bit differently, so treat the numbers as **estimates within ~10-20%** — good
enough to answer "does this fit in one prompt, or must I split it?".
"""

from __future__ import annotations

from collections.abc import Callable

# GPT-4o / o-series encoding; a reasonable proxy for other modern models too.
DEFAULT_ENCODING = "o200k_base"

# Rough context windows (tokens) for the fit/split verdict, keyed by a short
# alias. Ballpark only — providers move these; --budget always overrides.
MODEL_WINDOWS: dict[str, int] = {
    "claude-sonnet-5": 200_000,
    "gpt-5.5-instant": 128_000,
    "gpt-4o": 128_000,
    "gemini": 1_000_000,
}

# Conservative default budget (tokens): fits Claude Sonnet 5's standard window
# and leaves headroom below GPT-class 128k. Override with --budget.
DEFAULT_BUDGET = 128_000

_enc_cache: dict[str, object] = {}


def tiktoken_available() -> bool:
    """True if the optional ``tokens`` extra (tiktoken) is importable."""
    import importlib.util

    return importlib.util.find_spec("tiktoken") is not None


def _encoder(encoding: str = DEFAULT_ENCODING):
    try:
        import tiktoken
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via the CLI hint
        raise ModuleNotFoundError(
            "token counting needs tiktoken — install it with: uv sync --extra tokens"
        ) from exc
    enc = _enc_cache.get(encoding)
    if enc is None:
        enc = _enc_cache[encoding] = tiktoken.get_encoding(encoding)
    return enc


def count_tokens(text: str, encoding: str = DEFAULT_ENCODING) -> int:
    """Number of tokens in ``text`` (0 for empty). Needs the ``tokens`` extra."""
    if not text:
        return 0
    return len(_encoder(encoding).encode(text))


def fmt_kt(n_tokens: int) -> str:
    """Human 'thousands of tokens' string: 12345 -> ``'12.3 kt'``, 640 -> ``'0.6 kt'``."""
    return f"{n_tokens / 1000:.1f} kt"


def _hard_split(block: str, budget: int, count: Callable[[str], int]) -> list[str]:
    """Split one over-budget block by lines (then by chars for a monster line)."""
    parts: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for line in block.split("\n"):
        lt = count(line) if line else 0
        if lt > budget:
            # A single line beats the budget: flush, then chop it by characters.
            if cur:
                parts.append("\n".join(cur))
                cur, cur_tok = [], 0
            # ~4 chars per token is the usual ballpark; stay under budget.
            step = max(1, budget * 4)
            parts.extend(line[i : i + step] for i in range(0, len(line), step))
            continue
        if cur and cur_tok + lt > budget:
            parts.append("\n".join(cur))
            cur, cur_tok = [], 0
        cur.append(line)
        cur_tok += lt + 1  # +~1 for the rejoined newline
    if cur:
        parts.append("\n".join(cur))
    return parts


def split_by_budget(
    text: str,
    budget_tokens: int,
    count: Callable[[str], int] = count_tokens,
    sep: str = "\n\n",
) -> list[str]:
    """Split ``text`` into parts each within ``budget_tokens``.

    Greedy over blank-line-separated blocks, keeping whole blocks together (a
    block bigger than the budget is hard-split by lines). **Pure**: pass a
    ``count`` callable to test it without a tokenizer. ``budget_tokens <= 0`` (or
    text already within budget) returns ``[text]`` unchanged.

    Block costs are summed once (not re-tokenized per candidate), so the packing
    is O(n) — a couple of separator tokens of slack per join, well inside the
    estimate's own error bars.
    """
    if budget_tokens <= 0 or not text:
        return [text]
    if count(text) <= budget_tokens:
        return [text]

    parts: list[str] = []
    cur: list[str] = []
    cur_tok = 0
    for block in text.split(sep):
        bt = count(block) if block else 0
        if bt > budget_tokens:
            if cur:
                parts.append(sep.join(cur))
                cur, cur_tok = [], 0
            parts.extend(_hard_split(block, budget_tokens, count))
            continue
        if cur and cur_tok + bt > budget_tokens:
            parts.append(sep.join(cur))
            cur, cur_tok = [], 0
        cur.append(block)
        cur_tok += bt + 1  # +~1 for the rejoined separator
    if cur:
        parts.append(sep.join(cur))
    return parts
