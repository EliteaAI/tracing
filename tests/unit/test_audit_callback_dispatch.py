"""Unit tests: langchain_core dispatches tool events to the audit callback.

langchain_core reads ``handler.run_inline`` when it dispatches an async tool
start event. A handler without that attribute makes every async tool start
raise AttributeError, so callers fall back to the sync path.
"""
import asyncio
import importlib.util
import uuid

import pytest
from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler, CallbackManager
from langchain_core.tools import tool
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def callback_cls(utils_path):
    spec = importlib.util.spec_from_file_location(
        'audit_langchain_callback_dispatch', utils_path / 'audit_langchain_callback.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AuditLangChainCallback


@pytest.fixture
def audit(callback_cls):
    """A callback that writes to its own in-memory exporter, plus that exporter."""
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handler = callback_cls(user_id=42, project_id=7)
    handler._tracer = provider.get_tracer("audit-trail")
    return handler, exporter


def _tool_spans(exporter):
    return [
        span for span in exporter.get_finished_spans()
        if span.attributes.get("audit.observation.type") == "tool"
    ]


def test_handler_is_a_langchain_callback_handler(callback_cls):
    handler = callback_cls()
    assert isinstance(handler, BaseCallbackHandler)
    assert handler.run_inline is False


def test_async_tool_events_reach_the_handler(audit):
    handler, exporter = audit

    async def dispatch():
        manager = AsyncCallbackManager.configure(inheritable_callbacks=[handler])
        run = await manager.on_tool_start({"name": "create_pull_request"}, "{}", run_id=uuid.uuid4())
        await run.on_tool_end("done")

    asyncio.run(dispatch())

    spans = _tool_spans(exporter)
    assert [span.name for span in spans] == ["create_pull_request"]
    assert spans[0].attributes["audit.tool.name"] == "create_pull_request"
    assert spans[0].attributes["user.id"] == 42
    assert spans[0].attributes["project.id"] == 7
    assert "audit.duration_ms" in spans[0].attributes


def test_sync_tool_events_reach_the_handler(audit):
    handler, exporter = audit

    manager = CallbackManager.configure(inheritable_callbacks=[handler])
    run = manager.on_tool_start({"name": "delete_file"}, "{}", run_id=uuid.uuid4())
    run.on_tool_end("done")

    spans = _tool_spans(exporter)
    assert [span.name for span in spans] == ["delete_file"]
    assert "audit.duration_ms" in spans[0].attributes


def test_tool_error_marks_the_span_in_both_paths(audit):
    handler, exporter = audit

    async def dispatch():
        manager = AsyncCallbackManager.configure(inheritable_callbacks=[handler])
        run = await manager.on_tool_start({"name": "async_tool"}, "{}", run_id=uuid.uuid4())
        await run.on_tool_error(RuntimeError("boom"))

    asyncio.run(dispatch())
    manager = CallbackManager.configure(inheritable_callbacks=[handler])
    manager.on_tool_start({"name": "sync_tool"}, "{}", run_id=uuid.uuid4()).on_tool_error(RuntimeError("boom"))

    spans = _tool_spans(exporter)
    assert sorted(span.name for span in spans) == ["async_tool", "sync_tool"]
    assert all(span.attributes["audit.is_error"] is True for span in spans)


def test_real_tool_runs_record_a_span_through_ainvoke_and_invoke(audit):
    handler, exporter = audit

    @tool
    async def add_async(a: int, b: int) -> int:
        """Add two numbers with an async implementation."""
        return a + b

    @tool
    def add_sync(a: int, b: int) -> int:
        """Add two numbers with a sync implementation."""
        return a + b

    config = {"callbacks": [handler]}
    assert asyncio.run(add_async.ainvoke({"a": 1, "b": 2}, config=config)) == 3
    assert add_sync.invoke({"a": 3, "b": 4}, config=config) == 7

    spans = _tool_spans(exporter)
    assert sorted(span.name for span in spans) == ["add_async", "add_sync"]
