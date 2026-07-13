"""
Lightweight LangChain callback that creates OTEL spans for tool/LLM events.

Used as a fallback when Langfuse is not configured, ensuring tool calls
and LLM calls always appear in the audit trail.
"""

import time

from pylon.core.tools import log

AUDIT_TRACER_NAME = "audit-trail"


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
        self._tracer = trace.get_tracer(AUDIT_TRACER_NAME, "1.0.0")
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
                input_tokens, output_tokens = self._extract_tokens(response)
                if input_tokens is not None:
                    span.set_attribute("audit.input_tokens", input_tokens)
                if output_tokens is not None:
                    span.set_attribute("audit.output_tokens", output_tokens)
            finally:
                span.end()

    @staticmethod
    def _extract_tokens(response):
        """Extract (input_tokens, output_tokens) from a LangChain LLMResult."""
        try:
            if response is None:
                return None, None
            llm_out = getattr(response, 'llm_output', None) or {}
            tu = llm_out.get('token_usage') if isinstance(llm_out, dict) else None
            if tu:
                inp = tu.get('prompt_tokens') if 'prompt_tokens' in tu else tu.get('input_tokens')
                out = tu.get('completion_tokens') if 'completion_tokens' in tu else tu.get('output_tokens')
                if inp is not None or out is not None:
                    return inp, out
            generations = getattr(response, 'generations', None) or []
            for gen_list in generations:
                for gen in (gen_list if isinstance(gen_list, list) else [gen_list]):
                    msg = getattr(gen, 'message', None)
                    if msg is None:
                        continue
                    usage = getattr(msg, 'usage_metadata', None)
                    if usage and isinstance(usage, dict):
                        inp = usage.get('input_tokens') if 'input_tokens' in usage else usage.get('prompt_tokens')
                        out = usage.get('output_tokens') if 'output_tokens' in usage else usage.get('completion_tokens')
                        if inp is not None or out is not None:
                            return inp, out
            return None, None
        except Exception:
            return None, None

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
