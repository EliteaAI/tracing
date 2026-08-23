"""
Lightweight LangChain callback that creates OTEL spans for tool/LLM events.

Used as a fallback when Langfuse is not configured, ensuring tool calls
and LLM calls always appear in the audit trail.
"""

import json
import math
import os
import time

from pylon.core.tools import log

AUDIT_TRACER_NAME = "audit-trail"

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    with open(os.path.join(_PLUGIN_DIR, "metadata.json"), "r") as _f:
        _PLUGIN_VERSION = json.load(_f).get("version", "0.0.0")
except Exception:
    _PLUGIN_VERSION = "0.0.0"

# LangChain's UsageMetadata contract: input_tokens is the SUM of every input
# token type, and input_token_details is a breakdown of it, not an addition to
# it. The audit trail persists input NET of cache — analytics adds the cache
# columns back on top — so these buckets are subtracted before emitting.
_CACHE_READ_SUFFIXES = ("cache_read",)
_CACHE_CREATE_SUFFIXES = ("cache_creation",)
# langchain-anthropic also splits cache creation by TTL tier. A provider may
# report the total, the per-tier split, or both carrying the same tokens, so
# the two groups are counted separately and never added to each other.
_CACHE_CREATE_TIER_SUFFIXES = (
    "ephemeral_5m_input_tokens",
    "ephemeral_1h_input_tokens",
)
# Provider-native token_usage shapes, read when usage_metadata is absent or
# when it is missing a cache bucket the provider did report.
_RAW_CACHE_READ_KEYS = ("cached_tokens", "cache_read_input_tokens", "cache_read")
_RAW_CACHE_CREATE_KEYS = (
    "cache_creation_input_tokens", "cache_creation_tokens",
    "cache_write_tokens", "cache_creation",
)


def _first_positive_int(d, keys):
    """First positive int-coercible value under any of ``keys``, else 0."""
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            return count
    return 0


class AuditLangChainCallback:
    """Creates OTEL spans for tool/LLM events when Langfuse is not available.

    Implements the LangChain callback interface methods needed for audit tracking.
    Uses the platform's own OTEL tracer (not Langfuse) to create spans that
    flow through the AuditSpanProcessor.
    """

    raise_error: bool = False
    ignore_llm: bool = False
    ignore_chain: bool = True
    ignore_agent: bool = False
    ignore_retriever: bool = True
    ignore_chat_model: bool = False
    ignore_custom_event: bool = True
    ignore_retry: bool = True

    def __init__(self, user_id=None, user_email=None, project_id=None):
        from opentelemetry import trace
        self._tracer = trace.get_tracer(AUDIT_TRACER_NAME, _PLUGIN_VERSION)
        self._spans = {}
        self._start_times = {}
        # User context to propagate to every span
        self._user_attrs = {}
        if user_id is not None:
            try:
                self._user_attrs["user.id"] = int(user_id)
            except (TypeError, ValueError):
                pass
        if user_email:
            self._user_attrs["user.email"] = str(user_email)
        if project_id is not None:
            try:
                self._user_attrs["project.id"] = int(project_id)
            except (TypeError, ValueError):
                pass

    # -- Tool callbacks --

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        tool_name = serialized.get("name", "unknown_tool") if serialized else "unknown_tool"
        try:
            attrs = {
                "audit.observation.type": "tool",
                "audit.tool.name": tool_name,
            }
            attrs.update(self._user_attrs)
            span = self._tracer.start_span(
                name=tool_name,
                attributes=attrs,
            )
            key = str(run_id)
            self._spans[key] = span
            self._start_times[key] = time.perf_counter()
            log.debug("[AUDIT_TOOL_DEBUG] Tool started: %s run_id: %s", tool_name, run_id)
        except Exception as e:
            log.error(f"AuditLangChainCallback: failed to start tool span: {e}", exc_info=True)

    def on_tool_end(self, output, *, run_id, **kwargs):
        key = str(run_id)
        span = self._spans.pop(key, None)
        start = self._start_times.pop(key, None)
        if span:
            try:
                if start is not None:
                    span.set_attribute("audit.duration_ms", (time.perf_counter() - start) * 1000)
                span.end()
                log.debug("[AUDIT_TOOL_DEBUG] Tool ended: run_id: %s", run_id)
            except Exception:
                pass

    def on_tool_error(self, error, *, run_id, **kwargs):
        key = str(run_id)
        span = self._spans.pop(key, None)
        start = self._start_times.pop(key, None)
        if span:
            try:
                span.set_attribute("audit.is_error", True)
                if start is not None:
                    span.set_attribute("audit.duration_ms", (time.perf_counter() - start) * 1000)
                span.end()
                log.debug("[AUDIT_TOOL_DEBUG] Tool error: run_id: %s error: %s", run_id, error)
            except Exception:
                pass

    # -- LLM/Chat callbacks --

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        model = "unknown_model"
        if kwargs.get("invocation_params"):
            model = kwargs["invocation_params"].get("model_name", model)
            if model == "unknown_model":
                model = kwargs["invocation_params"].get("model", model)
        elif serialized:
            model = serialized.get("name", model)
        try:
            attrs = {
                "audit.observation.type": "generation",
                "audit.model.name": model,
            }
            attrs.update(self._user_attrs)
            span = self._tracer.start_span(
                name=model,
                attributes=attrs,
            )
            key = str(run_id)
            self._spans[key] = span
            self._start_times[key] = time.perf_counter()
        except Exception as e:
            log.error(f"AuditLangChainCallback: failed to start LLM span: {e}", exc_info=True)

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        model = "unknown_model"
        if kwargs.get("invocation_params"):
            model = kwargs["invocation_params"].get("model_name", model)
            if model == "unknown_model":
                model = kwargs["invocation_params"].get("model", model)
        elif serialized:
            model = serialized.get("name", model)
        try:
            attrs = {
                "audit.observation.type": "generation",
                "audit.model.name": model,
            }
            attrs.update(self._user_attrs)
            span = self._tracer.start_span(
                name=model,
                attributes=attrs,
            )
            key = str(run_id)
            self._spans[key] = span
            self._start_times[key] = time.perf_counter()
        except Exception as e:
            log.error(f"AuditLangChainCallback: failed to start LLM span: {e}", exc_info=True)

    def on_llm_end(self, response, *, run_id, **kwargs):
        key = str(run_id)
        span = self._spans.pop(key, None)
        start = self._start_times.pop(key, None)
        if span:
            try:
                if start is not None:
                    span.set_attribute("audit.duration_ms", (time.perf_counter() - start) * 1000)
                input_tokens, output_tokens, cache_read, cache_creation = \
                    self._extract_tokens(response)
                if input_tokens is not None:
                    span.set_attribute("audit.input_tokens", input_tokens)
                if output_tokens is not None:
                    span.set_attribute("audit.output_tokens", output_tokens)
                if cache_read:
                    span.set_attribute("audit.cache_read_tokens", cache_read)
                if cache_creation:
                    span.set_attribute("audit.cache_creation_tokens", cache_creation)
                response_cost = self._extract_response_cost(response)
                if response_cost is not None:
                    span.set_attribute("audit.llm_cost", response_cost)
            except Exception:
                log.debug("[AUDIT_LLM_DEBUG] failed to set span attrs: run_id: %s", run_id)
            finally:
                span.end()

    @staticmethod
    def _extract_response_cost(response):
        """Extract response_cost from LiteLLM's llm_output (OSS feature)."""
        try:
            if response is None:
                return None
            llm_out = getattr(response, 'llm_output', None) or {}
            if not isinstance(llm_out, dict):
                return None
            cost = llm_out.get('response_cost')
            if cost is not None:
                v = float(cost)
                return v if math.isfinite(v) else None
            hidden = llm_out.get('_hidden_params') or {}
            if isinstance(hidden, dict):
                cost = hidden.get('response_cost')
                if cost is not None:
                    v = float(cost)
                    return v if math.isfinite(v) else None
            return None
        except (TypeError, ValueError):
            return None

    @classmethod
    def _extract_tokens(cls, response):
        """Extract (input, output, cache_read, cache_creation) from an LLMResult.

        ``usage_metadata`` is read before ``llm_output["token_usage"]``: it is
        the provider-agnostic shape that carries the cache breakdown under
        ``input_token_details``, and it is the same source the Langfuse handler
        reads, so both audit paths persist identical numbers. The returned
        input count is NET of cache.
        """
        try:
            if response is None:
                return None, None, 0, 0
            generations = getattr(response, 'generations', None) or []
            for gen_list in generations:
                for gen in (gen_list if isinstance(gen_list, list) else [gen_list]):
                    msg = getattr(gen, 'message', None)
                    if msg is None:
                        continue
                    usage = getattr(msg, 'usage_metadata', None)
                    if not (usage and isinstance(usage, dict)):
                        continue
                    inp = usage.get('input_tokens')
                    if inp is None:
                        inp = usage.get('prompt_tokens')
                    out = usage.get('output_tokens')
                    if out is None:
                        out = usage.get('completion_tokens')
                    if inp is None and out is None:
                        continue
                    cache_read, cache_creation = cls._cache_from_usage_details(
                        usage.get('input_token_details')
                    )
                    if not (cache_read and cache_creation):
                        # langchain-openai maps only OpenAI's own cache keys, so
                        # an OpenAI-compatible proxy that names them differently
                        # (LiteLLM: cache_creation_tokens) leaves the breakdown
                        # out of usage_metadata while input_tokens still counts
                        # those tokens. Fill each empty bucket from the raw shape.
                        raw_read, raw_creation = cls._cache_from_raw_usage(
                            cls._raw_token_usage(response)
                        )
                        cache_read = cache_read or raw_read
                        cache_creation = cache_creation or raw_creation
                    return cls._net_of_cache(inp, out, cache_read, cache_creation)
            tu = cls._raw_token_usage(response)
            if tu:
                # Prefer the first populated value: a canonical key present but
                # None must not shadow a populated alias in the same dict.
                inp = tu.get('prompt_tokens')
                if inp is None:
                    inp = tu.get('input_tokens')
                out = tu.get('completion_tokens')
                if out is None:
                    out = tu.get('output_tokens')
                if inp is not None or out is not None:
                    cache_read, cache_creation = cls._cache_from_raw_usage(tu)
                    return cls._net_of_cache(inp, out, cache_read, cache_creation)
            return None, None, 0, 0
        except Exception:
            return None, None, 0, 0

    @staticmethod
    def _cache_from_usage_details(details):
        """Split the cache buckets out of a UsageMetadata input_token_details dict.

        Matched by suffix because langchain-openai prefixes the keys with the
        service tier ("priority_cache_read", "flex_cache_read"). Keys that are
        not cache buckets must never be counted — "audio", "reasoning", and the
        bare service-tier key, which holds input_tokens minus cache_read.

        Cache creation can arrive both as a total and split by TTL tier, each
        carrying the same tokens; the larger group wins so they never add up.
        """
        if not isinstance(details, dict):
            return 0, 0
        cache_read = 0
        creation_total = 0
        creation_tiered = 0
        for raw_key, raw_value in details.items():
            if not isinstance(raw_key, str) or raw_value is None:
                continue
            try:
                count = int(raw_value)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            if raw_key.endswith(_CACHE_READ_SUFFIXES):
                cache_read += count
            elif raw_key.endswith(_CACHE_CREATE_SUFFIXES):
                creation_total += count
            elif raw_key.endswith(_CACHE_CREATE_TIER_SUFFIXES):
                creation_tiered += count
        return cache_read, max(creation_total, creation_tiered)

    @staticmethod
    def _raw_token_usage(response):
        """The provider-native token_usage dict of an LLMResult, if present."""
        llm_out = getattr(response, 'llm_output', None) or {}
        if not isinstance(llm_out, dict):
            return None
        usage = llm_out.get('token_usage')
        return usage if isinstance(usage, dict) else None

    @staticmethod
    def _cache_from_raw_usage(usage):
        """Read the cache buckets of a provider-native token_usage dict.

        The same count is commonly repeated at the top level and inside the
        nested detail dicts, so the first populated source wins per bucket
        instead of the sources being added together.
        """
        if not isinstance(usage, dict):
            return 0, 0
        sources = [usage]
        tiered = 0
        for nested_key in ("prompt_tokens_details", "input_token_details"):
            nested = usage.get(nested_key)
            if not isinstance(nested, dict):
                continue
            sources.append(nested)
            tiers = nested.get("cache_creation_token_details")
            if isinstance(tiers, dict):
                for tier_key in _CACHE_CREATE_TIER_SUFFIXES:
                    try:
                        tiered += int(tiers.get(tier_key) or 0)
                    except (TypeError, ValueError):
                        continue
        cache_read = 0
        cache_creation = 0
        for source in sources:
            if not cache_read:
                cache_read = _first_positive_int(source, _RAW_CACHE_READ_KEYS)
            if not cache_creation:
                cache_creation = _first_positive_int(source, _RAW_CACHE_CREATE_KEYS)
        return cache_read, cache_creation or max(tiered, 0)

    @staticmethod
    def _net_of_cache(input_tokens, output_tokens, cache_read, cache_creation):
        """Subtract the cache buckets from a cache-inclusive input count.

        A provider that already reports input exclusive of cache — its input
        total would be smaller than the buckets it reports alongside it — is
        left untouched.
        """
        cache_total = cache_read + cache_creation
        if input_tokens is not None and cache_total:
            try:
                total = int(input_tokens)
            except (TypeError, ValueError):
                return input_tokens, output_tokens, cache_read, cache_creation
            if total >= cache_total:
                input_tokens = total - cache_total
        return input_tokens, output_tokens, cache_read, cache_creation

    def on_llm_error(self, error, *, run_id, **kwargs):
        key = str(run_id)
        span = self._spans.pop(key, None)
        start = self._start_times.pop(key, None)
        if span:
            try:
                span.set_attribute("audit.is_error", True)
                if start is not None:
                    span.set_attribute("audit.duration_ms", (time.perf_counter() - start) * 1000)
                span.end()
            except Exception:
                pass

    def on_llm_new_token(self, token, *, run_id, **kwargs):
        pass

    # -- No-op stubs for other callbacks --

    def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
        pass

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        pass

    def on_chain_error(self, error, *, run_id, **kwargs):
        pass
