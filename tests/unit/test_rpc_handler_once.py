"""Exercise owning tracing wrappers: business handlers must run at most once."""
import ast
import asyncio
import importlib.util
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock

import pytest


@pytest.fixture
def wrapper(utils_path, monkeypatch):
    trace = ModuleType('opentelemetry.trace')
    trace.SpanKind = NS(SERVER='server')
    trace.StatusCode = NS(OK='ok', ERROR='error')
    trace.Status = lambda *args: args
    monkeypatch.setitem(sys.modules, 'opentelemetry.trace', trace)
    spec = importlib.util.spec_from_file_location('rpc_server_once', utils_path / 'rpc_server_trace.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return lambda tracer: module.create_rpc_server_wrapper(
        tracer, capture_payload=False, capture_user_context=False)


class Tracer:
    def __init__(self, failure=None):
        self.failure = failure
        self.span = Mock()
        if failure == 'record':
            self.span.record_exception.side_effect = RuntimeError('trace recording failed')
        if failure == 'metadata':
            self.span.set_attribute.side_effect = RuntimeError('trace attributes failed')

    def start_as_current_span(self, *args, **kwargs):
        if self.failure == 'setup':
            raise RuntimeError('trace setup failed')
        return self

    def __enter__(self):
        if self.failure == 'enter':
            raise RuntimeError('span entry failed')
        return self.span

    def __exit__(self, *args):
        if self.failure == 'exit':
            raise RuntimeError('span exit failed')
        return False


@pytest.mark.parametrize('error_type', [ValueError, ImportError, KeyboardInterrupt, asyncio.CancelledError])
@pytest.mark.parametrize('trace_failure', [None, 'record', 'metadata', 'exit'])
def test_original_business_exception_is_preserved_once(wrapper, error_type, trace_failure):
    error = error_type('business failure')
    handler = Mock(side_effect=error)
    with pytest.raises(error_type) as caught:
        wrapper(Tracer(trace_failure))('create_message')(handler)(1, project_id=2)
    assert caught.value is error
    handler.assert_called_once_with(1, project_id=2)


@pytest.mark.parametrize('failure', ['setup', 'enter'])
def test_pre_entry_instrumentation_failure_keeps_one_untraced_call(wrapper, failure):
    handler = Mock(return_value={'created': 1})
    assert wrapper(Tracer(failure))('create_message')(handler)() == {'created': 1}
    handler.assert_called_once_with()


@pytest.mark.parametrize('result', [None, {'created': 1}])
@pytest.mark.parametrize('failure', [None, 'metadata', 'exit'])
def test_successful_result_survives_post_call_instrumentation_failure(wrapper, result, failure):
    handler = Mock(return_value=result)
    assert wrapper(Tracer(failure))('create_message')(handler)() is result
    handler.assert_called_once_with()


def test_no_tracer_and_nested_rpc_business_failure_do_not_repeat(wrapper):
    error = ValueError('invalid selection')
    application = Mock(side_effect=error)
    traced_application = wrapper(Tracer())('application_predict')(application)
    chat = Mock(side_effect=traced_application)
    with pytest.raises(ValueError) as caught:
        wrapper(Tracer())('chat_predict')(chat)()
    assert caught.value is error
    application.assert_called_once_with()
    chat.assert_called_once_with()
    plain = Mock(return_value=1)
    assert wrapper(None)('untraced')(plain)() == 1
    plain.assert_called_once_with()


@pytest.fixture
def register(plugin_root, monkeypatch):
    """Compile the actual registration method without importing plugin startup."""
    class Node:
        def register(self, handler, name=None, *args, **kwargs):
            self.handler = handler

    arbiter = ModuleType('arbiter')
    arbiter.RpcNode = Node
    monkeypatch.setitem(sys.modules, 'arbiter', arbiter)
    path = plugin_root / 'module.py'
    cls = next(n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'Module')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_patch_rpc_registration')
    namespace = {'log': Mock()}
    exec(compile(ast.Module([method], []), str(path), 'exec'), namespace)

    def prepare(factory, handler):
        owner = NS(_rpc_server_wrapper=factory, _init_rpc_server_tracing=Mock())
        namespace['_patch_rpc_registration'](owner)
        node = Node()
        node.register(handler, 'create_message')
        return node.handler
    return prepare


def test_lazy_registration_falls_back_only_for_wrapper_construction(register):
    handler = Mock(return_value={'created': 1})
    factory = Mock(side_effect=RuntimeError('wrapper unavailable'))
    assert register(factory, handler)() == {'created': 1}
    handler.assert_called_once_with()


@pytest.mark.parametrize('error_type', [ValueError, ImportError])
def test_lazy_registration_propagates_invocation_failure_without_fallback(register, wrapper, error_type):
    error = error_type('business failure')
    handler = Mock(side_effect=error)
    with pytest.raises(error_type) as caught:
        register(wrapper(Tracer()), handler)()
    assert caught.value is error
    handler.assert_called_once_with()


def test_lazy_registration_without_tracer_preserves_exception_once(register):
    error = ValueError('business failure')
    handler = Mock(side_effect=error)
    with pytest.raises(ValueError) as caught:
        register(None, handler)()
    assert caught.value is error
    handler.assert_called_once_with()
