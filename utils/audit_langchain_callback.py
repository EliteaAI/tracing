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
            log.info(f"[AUDIT_TOOL_DEBUG] Tool started: {tool_name}, run_id: {run_id}, user_attrs: {self._user_attrs}")
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
                log.info(f"[AUDIT_TOOL_DEBUG] Tool ended: run_id: {run_id}, duration: {(time.perf_counter() - start) * 1000 if start else 'N/A'}ms")
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
                log.info(f"[AUDIT_TOOL_DEBUG] Tool error: run_id: {run_id}, error: {error}")
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
                span.end()
            except Exception:
                pass

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
