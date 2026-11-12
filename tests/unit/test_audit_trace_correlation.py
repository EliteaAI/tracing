"""Unit tests: audit callback trace correlation contract."""
import contextvars
import importlib.util
import threading

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def callback_cls(utils_path):
    spec = importlib.util.spec_from_file_location(
        'audit_langchain_callback_correlation', utils_path / 'audit_langchain_callback.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AuditLangChainCallback


def test_audit_generation_is_a_child_of_owning_application_without_content(callback_cls):
    """Audit callback preserves attribution without storing prompt or output content."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    callback = callback_cls(user_id=42, project_id=7)
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
