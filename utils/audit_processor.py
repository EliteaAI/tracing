"""
AuditSpanProcessor — extracts audit events from completed OTEL spans.

on_end() extracts audit data synchronously (needs live span object),
then enqueues the lightweight audit_data dict for a background worker
thread that performs the actual write_fn (DB write / EventNode emit).

This keeps the request thread fast while ensuring span data is captured
before the span object is recycled by the OTEL SDK.

Supports spans from:
- Flask middleware (api_traces)
- Socket.IO wrapper (socket_io)
- RPC instrumentation (rpc_calls)
- Task wrapper (task_execution / agent runs)
- Schedule daemon (schedule_execution)
- Admin tasks (admin_task_execution)
- Langfuse tool/LLM spans (langfuse.observation.type)
- AuditLangChainCallback spans (audit.observation.type)
"""

import json
import threading
from queue import SimpleQueue

from pylon.core.tools import log
from .user_context import resolve_user_email


# Attribute keys (mirrored from user_context.py / data_types.py)
_DATA_TYPE_KEY = "telemetry.data_type"
_USER_ID_KEY = "user.id"
_USER_EMAIL_KEY = "user.email"
_PROJECT_ID_KEY = "project.id"

# Default paths to skip
_DEFAULT_SKIP_PATHS = frozenset([
    "/health", "/metrics", "/favicon.ico", "/static", "/admin/app/",
    "/llm/",  # LLM proxy — already captured as "llm" events via Langfuse/audit callback
])

# Sentinel to signal the worker to stop
_STOP = object()


# Langfuse's LangChain CallbackHandler emits usage/cost as JSON-serialized dicts
# under langfuse.observation.usage_details / .cost_details. Key shapes vary by
# provider — OpenAI keeps prompt_tokens/completion_tokens verbatim, everything
# else is normalized to input/output/total. See langfuse.langchain.CallbackHandler.
_LANGFUSE_INPUT_KEYS = ("input", "prompt_tokens", "input_tokens")
_LANGFUSE_OUTPUT_KEYS = ("output", "completion_tokens", "output_tokens")
_LANGFUSE_CACHE_READ_KEYS = (
    "input_cached_tokens",              # OpenAI-bypass shape
    "input_cache_read_input_tokens",    # Anthropic normalized shape
    "cache_read_input_tokens",
)
_LANGFUSE_CACHE_CREATE_KEYS = (
    "input_cache_creation_input_tokens",
    "cache_creation_input_tokens",
)
# Cost-detail keys we know how to sum. Any other key is dropped with a warning
# so future Langfuse schema drift can't silently inflate totals.
_ALLOWED_COST_KEYS = frozenset((
    "input", "output", "total",
    "cache_read_input", "cache_creation_input", "cache_read", "reasoning",
))


def _parse_langfuse_json_attr(raw):
    """Decode a langfuse.observation.* JSON-string attribute value.

    Langfuse serializes dicts via json.dumps before setting them on the OTEL
    span. Returns None on any parse failure so the caller can silently fall
    back to the audit.* namespace.
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        log.debug("audit: langfuse observation attr parse failed")
        return None
    return parsed if isinstance(parsed, dict) else None


def _pick_int(d, keys):
    """Return the first int-coercible value under any of ``keys``, else None."""
    if not d:
        return None
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


_unknown_cost_keys_seen = set()  # dedupe per-key WARN logs


def _pick_cost(cost_dict):
    """Extract a scalar USD cost from a Langfuse cost_details dict.

    Prefers a ``total`` key when it holds a valid number; otherwise sums
    values under keys in ``_ALLOWED_COST_KEYS``. Unknown keys are dropped
    (with a one-shot WARN) to guard against silent inflation on Langfuse
    schema drift.
    """
    if not cost_dict:
        return None
    total_raw = cost_dict.get("total")
    if total_raw is not None:
        try:
            return float(total_raw)
        except (TypeError, ValueError):
            # Fall through to the breakdown-sum path — real payloads
            # occasionally carry {"total": null, "input": X, "output": Y}.
            pass
    total = 0.0
    saw_number = False
    for k, v in cost_dict.items():
        if k == "total":
            continue
        if k not in _ALLOWED_COST_KEYS:
            if k not in _unknown_cost_keys_seen:
                _unknown_cost_keys_seen.add(k)
                log.warning("audit: unknown cost_details key ignored: %s", k)
            continue
        try:
            total += float(v)
            saw_number = True
        except (TypeError, ValueError):
            continue
    return total if saw_number else None


class AuditSpanProcessor:
    """OpenTelemetry SpanProcessor that captures audit-worthy spans.

    on_end() is called synchronously on the request thread. It extracts
    audit data from the span (which must happen inline before the span
    is recycled), then enqueues the extracted dict. A single daemon
    worker thread drains the queue and calls write_fn (DB write, etc.)
    off the request path.

    The worker thread is started lazily on the first on_end() call to
    survive pre-fork worker models (gunicorn/uWSGI) where threads
    created before fork are lost in child processes.
    """

    def __init__(self, write_fn, config=None):
        config = config or {}
        self.write_fn = write_fn
        self.skip_paths = set(config.get("skip_paths", _DEFAULT_SKIP_PATHS))
        self.audit_all_methods = config.get("audit_all_methods", False)

        self._queue = SimpleQueue()
        self._worker = None
        self._lock = threading.Lock()

    def _ensure_worker(self):
        """Start the drain worker if not already running (lazy, post-fork safe)."""
        if self._worker is not None and self._worker.is_alive():
            return
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._drain, daemon=True, name="audit-span-worker"
            )
            self._worker.start()

    # --- SpanProcessor interface (called on the request thread) ---

    def on_start(self, span, parent_context=None):
        pass

    def _on_ending(self, span):
        pass  # on_end handles extraction; _on_ending is a no-op to avoid double processing

    def on_end(self, span):
        """Extract audit data from span inline, enqueue for background write."""
        try:
            attrs = dict(span.attributes) if span.attributes else {}

            # Quick filter: skip spans we'll never care about
            data_type = attrs.get(_DATA_TYPE_KEY)
            obs_type = attrs.get("langfuse.observation.type")
            audit_obs_type = attrs.get("audit.observation.type")

            if not (data_type or obs_type or audit_obs_type):
                return

            # Snapshot the immutable bits we need (span object may be recycled)
            span_ctx = span.get_span_context()
            snapshot = {
                "name": span.name,
                "attrs": attrs,
                "trace_id": span_ctx.trace_id,
                "span_id": span_ctx.span_id,
                "parent_span_id": (
                    span.parent.span_id if hasattr(span, 'parent') and span.parent else None
                ),
                "start_time": getattr(span, 'start_time', None),
                "end_time": getattr(span, 'end_time', None),
                "status_ok": (
                    span.status.is_ok if hasattr(span, 'status') else True
                ),
            }

            # Extract synchronously (needs span data), enqueue result
            audit_data = self._extract(snapshot)
            if audit_data:
                self._ensure_worker()
                self._queue.put(audit_data)
        except Exception as e:
            log.debug(f"AuditSpanProcessor.on_end error: {e}")

    def shutdown(self):
        if self._worker is not None and self._worker.is_alive():
            self._queue.put(_STOP)
            self._worker.join(timeout=5)

    def force_flush(self, timeout_millis=None):
        # Nothing to flush (events are enqueued in on_end), but return True, not
        # None: a falsy result can make a multi-processor force_flush treat this
        # as failure and skip later processors, e.g. Langfuse (#5391).
        return True

    # --- Background worker (own thread — blocking write_fn is fine here) ---

    def _drain(self):
        """Process queued audit_data dicts until shutdown."""
        while True:
            try:
                item = self._queue.get()
                if item is _STOP:
                    break
                self.write_fn(item)
            except Exception as e:
                log.debug(f"AuditSpanProcessor worker error: {e}")

    # --- Internal extraction (runs on request thread) ---

    def _extract(self, snap):
        attrs = snap["attrs"]
        data_type = attrs.get(_DATA_TYPE_KEY)

        obs_type = attrs.get("langfuse.observation.type")
        audit_obs_type = attrs.get("audit.observation.type")

        if data_type == "api_traces":
            return self._extract_api(snap, attrs)
        elif data_type == "socket_io":
            return self._extract_sio(snap, attrs)
        elif data_type == "rpc_calls":
            return self._extract_rpc(snap, attrs)
        elif data_type == "task_execution":
            return self._extract_agent(snap, attrs)
        elif data_type == "schedule_execution":
            return self._extract_schedule(snap, attrs)
        elif data_type == "admin_task_execution":
            return self._extract_admin_task(snap, attrs)
        elif obs_type == "tool" or audit_obs_type == "tool":
            return self._extract_tool(snap, attrs)
        elif obs_type == "generation" or audit_obs_type == "generation":
            return self._extract_llm(snap, attrs)
        return None

    # --- Per-type extractors ---

    def _extract_api(self, snap, attrs):
        http_target = attrs.get("http.target", "")
        http_method = attrs.get("http.method", "")

        for skip in self.skip_paths:
            if http_target.startswith(skip):
                return None

        if not self.audit_all_methods and http_method == "GET":
            return None

        status_code = attrs.get("http.status_code")
        duration_ms = attrs.get("http.duration_ms")
        http_route = attrs.get("http.route", http_target)

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="api",
            action=snap["name"],
            http_method=http_method,
            http_route=http_route,
            status_code=int(status_code) if status_code is not None else None,
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=int(status_code) >= 400 if status_code is not None else False,
        )

    _SYSTEM_SIO_EVENTS = frozenset([
        "task_logs_subscribe",
        "task_logs_unsubscribe",
    ])

    def _extract_sio(self, snap, attrs):
        duration_ms = attrs.get("messaging.duration_ms")
        has_error = "error.message" in attrs

        # Classify admin infrastructure SIO events as system events
        action = snap["name"]
        event_type = "socketio"
        for sys_event in self._SYSTEM_SIO_EVENTS:
            if sys_event in action:
                event_type = "admin_task"
                break

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type=event_type,
            action=snap["name"],
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=has_error,
        )

    def _extract_rpc(self, snap, attrs):
        rpc_method = attrs.get("rpc.method", "")

        _AUDIT_RPC_PREFIXES = (
            "predict_sio", "predict_sio_llm", "predict_agent",
            "chat_predict_sio", "chat_continue_predict_sio",
            "applications_predict_sio", "applications_predict_sio_llm",
            "datasources_predict_sio",
            "applications_test_toolkit_tool_sio",
            "test_toolkit_tool_sio", "test_mcp_connection_sio",
            "mcp_sync_tools_sio",
        )
        if not any(rpc_method.startswith(p) for p in _AUDIT_RPC_PREFIXES):
            return None

        duration_ms = attrs.get("rpc.duration_ms")
        has_error = "rpc.error" in attrs

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="rpc",
            action=snap["name"],
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=has_error,
        )

    def _extract_agent(self, snap, attrs):
        duration_ms = attrs.get("task.duration_ms")

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="agent",
            action=snap["name"],
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=not snap["status_ok"],
        )

    def _extract_schedule(self, snap, attrs):
        duration_ms = attrs.get("schedule.duration_ms")

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="schedule",
            action=snap["name"],
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=not snap["status_ok"],
        )

    def _extract_admin_task(self, snap, attrs):
        duration_ms = attrs.get("task.duration_ms")

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="admin_task",
            action=snap["name"],
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=not snap["status_ok"],
        )

    def _extract_tool(self, snap, attrs):
        tool_name = (
            attrs.get("langfuse.observation.name")
            or attrs.get("audit.tool.name")
            or snap["name"]
        )
        duration_ms = attrs.get("audit.duration_ms")

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="tool",
            action=snap["name"],
            tool_name=tool_name,
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=bool(attrs.get("audit.is_error", False)),
        )

    def _extract_llm(self, snap, attrs):
        model_name = (
            attrs.get("langfuse.observation.model.name")
            or attrs.get("gen_ai.request.model")
            or attrs.get("audit.model.name")
            or snap["name"]
        )
        duration_ms = attrs.get("audit.duration_ms")

        # 1) Langfuse namespace (primary path in production — CallbackHandler
        #    emits usage_details / cost_details as JSON strings on every
        #    generation span). Silently falls through on any parse failure.
        usage_dict = _parse_langfuse_json_attr(
            attrs.get("langfuse.observation.usage_details")
        )
        cost_dict = _parse_langfuse_json_attr(
            attrs.get("langfuse.observation.cost_details")
        )

        input_tokens = _pick_int(usage_dict, _LANGFUSE_INPUT_KEYS)
        output_tokens = _pick_int(usage_dict, _LANGFUSE_OUTPUT_KEYS)
        cache_read_tokens = _pick_int(usage_dict, _LANGFUSE_CACHE_READ_KEYS) or 0
        cache_creation_tokens = _pick_int(usage_dict, _LANGFUSE_CACHE_CREATE_KEYS) or 0
        llm_cost = _pick_cost(cost_dict)
        token_source = "langfuse" if (
            input_tokens is not None or output_tokens is not None
        ) else None

        # 2) audit.* namespace — populated by AuditLangChainCallback when
        #    Langfuse credentials are absent. Fills in whichever fields the
        #    Langfuse path didn't already produce.
        if input_tokens is None:
            raw_in = attrs.get("audit.input_tokens")
            try:
                input_tokens = int(raw_in) if raw_in is not None else None
                if input_tokens is not None and token_source is None:
                    token_source = "audit"
            except (ValueError, TypeError):
                input_tokens = None
        if output_tokens is None:
            raw_out = attrs.get("audit.output_tokens")
            try:
                output_tokens = int(raw_out) if raw_out is not None else None
                if output_tokens is not None and token_source is None:
                    token_source = "audit"
            except (ValueError, TypeError):
                output_tokens = None
        if llm_cost is None:
            raw_cost = attrs.get("audit.llm_cost")
            try:
                llm_cost = float(raw_cost) if raw_cost is not None else None
            except (ValueError, TypeError):
                llm_cost = None

        cost_source = "observed" if llm_cost is not None else None
        # 3) If neither namespace supplied a cost, estimate from tokens against
        #    the vendored LiteLLM pricing table. Cached models used to charge
        #    nothing under Langfuse-server-side costing today; this fills that
        #    gap for the non-Langfuse env.
        if llm_cost is None:
            try:
                from .model_pricing import compute_llm_cost
                llm_cost, cost_source = compute_llm_cost(
                    model_name, input_tokens, output_tokens,
                    cache_read_input_tokens=cache_read_tokens,
                    cache_creation_input_tokens=cache_creation_tokens,
                )
            except Exception as e:
                log.debug("audit: cost estimation failed: %s", e)

        return self._build_event(
            snap=snap,
            attrs=attrs,
            event_type="llm",
            action=snap["name"],
            model_name=model_name,
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            is_error=bool(attrs.get("audit.is_error", False)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            llm_cost=llm_cost,
            token_source=token_source,
            cost_source=cost_source,
        )

    # --- Event builder (runs on worker thread — blocking RPC is fine) ---

    def _build_event(self, snap, attrs, event_type, action, **extra):
        """Build a dict suitable for writing as an AuditEvent row."""
        trace_id = format(snap["trace_id"], '032x') if snap["trace_id"] else None
        span_id = format(snap["span_id"], '016x') if snap["span_id"] else None
        parent_span_id = (
            format(snap["parent_span_id"], '016x') if snap["parent_span_id"] else None
        )

        # Extract user/project from span attributes
        user_id = attrs.get(_USER_ID_KEY)
        user_email = attrs.get(_USER_EMAIL_KEY)
        project_id = attrs.get(_PROJECT_ID_KEY)

        # Fallback: check Langfuse trace metadata for project_id
        if project_id is None:
            project_id = (
                attrs.get("langfuse.trace.metadata.project_id")
                or attrs.get("langfuse.trace.metadata.chat_project_id")
            )

        # Convert to proper types
        try:
            user_id = int(user_id) if user_id is not None else None
        except (TypeError, ValueError):
            user_id = None
        try:
            project_id = int(project_id) if project_id is not None else None
        except (TypeError, ValueError):
            project_id = None

        # Resolve user email (blocking RPC with cache — fine on worker thread)
        if not user_email and user_id is not None:
            user_email = resolve_user_email(user_id)

        # Build timestamp from span end time (nanoseconds since epoch)
        timestamp = None
        end_time = snap.get("end_time")
        if end_time:
            from datetime import datetime, timezone
            timestamp = datetime.fromtimestamp(end_time / 1e9, tz=timezone.utc)

        event = {
            "timestamp": timestamp,
            "user_id": user_id,
            "user_email": str(user_email) if user_email else None,
            "project_id": project_id,
            "event_type": event_type,
            "action": str(action)[:512],
            "trace_id": trace_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "is_error": extra.pop("is_error", False),
        }

        # Extract entity context from span attributes
        entity_type = attrs.get("entity.type")
        if entity_type:
            event["entity_type"] = str(entity_type)[:32]
            entity_id = attrs.get("entity.id")
            if entity_id is not None:
                try:
                    event["entity_id"] = int(entity_id)
                except (TypeError, ValueError):
                    pass
            entity_name = attrs.get("entity.name")
            if entity_name:
                event["entity_name"] = str(entity_name)[:256]

        # Add optional fields
        for key in ("http_method", "http_route", "status_code", "duration_ms",
                     "tool_name", "model_name", "input_tokens", "output_tokens",
                     "llm_cost", "token_source", "cost_source"):
            if key in extra and extra[key] is not None:
                event[key] = extra[key]

        # Fallback: calculate duration from span timestamps if not set
        if "duration_ms" not in event or event.get("duration_ms") is None:
            try:
                start_time = snap.get("start_time")
                if start_time and end_time:
                    event["duration_ms"] = (end_time - start_time) / 1e6
            except Exception:
                pass

        return event
