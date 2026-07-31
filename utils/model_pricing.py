"""LLM cost estimation from a vendored LiteLLM pricing table.

Data source: model_prices.json alongside this module — byte-for-byte from
LiteLLM's model_prices_and_context_window.json at the pinned release tag.
Refresh procedure: see PRICING.md.
"""

import json
import os

from pylon.core.tools import log

_PRICE_TABLE_VERSION = "litellm-v1.83.14-stable"
_PRICES_PATH = os.path.join(os.path.dirname(__file__), "model_prices.json")

_prices_cache = None
_unknown_models = set()  # dedupe per-model WARN logs

# Provider prefixes we strip when the exact model_name isn't in the table.
# LiteLLM keys sometimes carry the provider prefix and sometimes don't; the
# routes-with-region prefixes (us./eu./apac./ca.) are the Bedrock inference-
# profile convention that our proxy passes through.
_STRIP_PREFIXES = (
    "openai/", "azure/", "anthropic/", "bedrock/", "vertex_ai/", "gemini/",
    "us.", "eu.", "apac.", "ca.",
)


def _load():
    global _prices_cache
    if _prices_cache is not None:
        return _prices_cache
    try:
        with open(_PRICES_PATH, "r") as f:
            _prices_cache = json.load(f)
    except (OSError, ValueError) as e:
        log.error("model_pricing: failed to load %s: %s", _PRICES_PATH, e)
        _prices_cache = {}
    return _prices_cache


def _lookup(model_name):
    """Try exact match, lowercase, then strip common provider prefixes."""
    if not model_name:
        return None
    prices = _load()
    if model_name in prices:
        return prices[model_name]
    lowered = model_name.lower()
    if lowered in prices:
        return prices[lowered]
    for prefix in _STRIP_PREFIXES:
        if model_name.startswith(prefix):
            stripped = model_name[len(prefix):]
            if stripped in prices:
                return prices[stripped]
        if lowered.startswith(prefix):
            stripped = lowered[len(prefix):]
            if stripped in prices:
                return prices[stripped]
    return None


def compute_llm_cost(model_name, input_tokens, output_tokens,
                     cache_read_input_tokens=0,
                     cache_creation_input_tokens=0):
    """Estimate LLM cost in USD from token counts.

    Returns (cost_usd, cost_source_tag) or (None, None) if the model is
    unpriced. Cache-read/creation tokens are priced separately when the
    entry has those keys; otherwise they fall back to the base input rate.
    """
    if not model_name or (not input_tokens and not output_tokens):
        return None, None

    entry = _lookup(model_name)
    if not entry or not isinstance(entry, dict):
        if model_name not in _unknown_models:
            _unknown_models.add(model_name)
            log.warning("model_pricing: no entry for %r", model_name)
        return None, None

    input_price = entry.get("input_cost_per_token")
    output_price = entry.get("output_cost_per_token")
    if input_price is None and output_price is None:
        # Embeddings/image/audio-only entries — not billable as chat.
        return None, None

    input_price = input_price or 0.0
    output_price = output_price or 0.0
    cache_read_price = entry.get("cache_read_input_token_cost", input_price)
    cache_create_price = entry.get("cache_creation_input_token_cost", input_price)

    # Convention: input_tokens is treated as already-net-of-cache (matches the
    # Langfuse normalized shape, which decrements the cache-read count from
    # "input"). The OpenAI-bypass shape keeps prompt_tokens inclusive of cache,
    # so on that path input_tokens will be slightly over-counted at the base
    # rate — a small, known bias documented in PRICING.md.
    cost = (
        (input_tokens or 0) * input_price
        + (output_tokens or 0) * output_price
        + (cache_read_input_tokens or 0) * cache_read_price
        + (cache_creation_input_tokens or 0) * cache_create_price
    )
    return cost, "estimated:" + _PRICE_TABLE_VERSION
