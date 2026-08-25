"""Audit callback trace correlation contract.

Run in the indexer runtime, where OpenTelemetry SDK is supplied by sdk_plugin:
    PYTHONPATH=/data/requirements/sdk_plugin/lib/python3.12/site-packages \
        python3 -m pytest utils/tests/test_audit_trace_correlation.py -q
"""

import contextvars
import pathlib
import sys
import threading
import types
import unittest.mock as mock

import pytest


pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


_pylon = types.ModuleType("pylon")
_pylon_core = types.ModuleType("pylon.core")
_pylon_tools = types.ModuleType("pylon.core.tools")
_pylon_tools.log = mock.MagicMock()
_pylon.core = _pylon_core
_pylon_core.tools = _pylon_tools
sys.modules.setdefault("pylon", _pylon)
sys.modules.setdefault("pylon.core", _pylon_core)
sys.modules.setdefault("pylon.core.tools", _pylon_tools)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from audit_langchain_callback import AuditLangChainCallback


def test_audit_generation_is_a_child_of_owning_application_without_content():
    """Audit callback preserves attribution without storing prompt or output content."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    callback = AuditLangChainCallback(user_id=42, project_id=7)
    tracer = trace.get_tracer("suggestion-audit-contract")

    with tracer.start_as_current_span("application") as application_span:
        invocation_context = contextvars.copy_context()

    def invoke_suggestion():
        callback.on_llm_start({}, ["private suggestion prompt"], run_id="suggestion-run")
        callback.on_llm_end(None, run_id="suggestion-run")

    thread = threading.Thread(target=lambda: invocation_context.run(invoke_suggestion))
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    generation = spans["unknown_model"]

    assert generation.context.trace_id == application_span.get_span_context().trace_id
    assert generation.parent.span_id == application_span.get_span_context().span_id
    assert generation.attributes["audit.observation.type"] == "generation"
    assert generation.attributes["user.id"] == 42
    assert generation.attributes["project.id"] == 7
    assert "private suggestion prompt" not in generation.attributes.values()