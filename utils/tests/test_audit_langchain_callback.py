"""
Unit tests for AuditLangChainCallback._extract_tokens.

Run standalone:
    cd tracing && python3 -m pytest utils/tests/test_audit_langchain_callback.py -v
"""

import sys
import types
import pathlib
import unittest.mock as mock

# Stub pylon.core.tools.log before importing the module under test
_pylon = types.ModuleType("pylon")
_pylon_core = types.ModuleType("pylon.core")
_pylon_tools = types.ModuleType("pylon.core.tools")
_pylon_tools.log = mock.MagicMock()
_pylon_tools.module = mock.MagicMock()
_pylon.core = _pylon_core
_pylon_core.tools = _pylon_tools
sys.modules.setdefault("pylon", _pylon)
sys.modules.setdefault("pylon.core", _pylon_core)
sys.modules.setdefault("pylon.core.tools", _pylon_tools)
sys.modules.setdefault("pylon.core.tools.log", _pylon_tools.log)
sys.modules.setdefault("pylon.core.tools.module", _pylon_tools.module)

# Stub opentelemetry.trace for the __init__
_otel = types.ModuleType("opentelemetry")
_otel_trace = types.ModuleType("opentelemetry.trace")
_otel_trace.get_tracer = mock.MagicMock()
_otel.trace = _otel_trace
sys.modules.setdefault("opentelemetry", _otel)
sys.modules.setdefault("opentelemetry.trace", _otel_trace)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


class _FakeMessage:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata


class _FakeGeneration:
    def __init__(self, message=None):
        self.message = message


class _FakeResponse:
    def __init__(self, llm_output=None, generations=None):
        self.llm_output = llm_output
        self.generations = generations or []


from audit_langchain_callback import AuditLangChainCallback
_extract = AuditLangChainCallback._extract_tokens


def test_none_response():
    assert _extract(None) == (None, None), "None input must return (None, None)"


def test_openai_style_llm_output():
    resp = _FakeResponse(llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    inp, out = _extract(resp)
    assert inp == 100, f"expected 100 input tokens, got {inp}"
    assert out == 50, f"expected 50 output tokens, got {out}"


def test_anthropic_style_llm_output():
    """Anthropic uses input_tokens / output_tokens key names."""
    resp = _FakeResponse(llm_output={"token_usage": {"input_tokens": 200, "output_tokens": 80}})
    inp, out = _extract(resp)
    assert inp == 200, inp
    assert out == 80, out


def test_usage_metadata_modern():
    """Modern models expose tokens via generation.message.usage_metadata."""
    msg = _FakeMessage(usage_metadata={"input_tokens": 300, "output_tokens": 120})
    gen = _FakeGeneration(message=msg)
    resp = _FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp == 300, inp
    assert out == 120, out


def test_usage_metadata_prompt_completion_keys():
    """Fallback key names inside usage_metadata."""
    msg = _FakeMessage(usage_metadata={"prompt_tokens": 50, "completion_tokens": 20})
    gen = _FakeGeneration(message=msg)
    resp = _FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp == 50, inp
    assert out == 20, out


def test_llm_output_preferred_over_usage_metadata():
    """llm_output takes priority when both present."""
    msg = _FakeMessage(usage_metadata={"input_tokens": 999, "output_tokens": 999})
    gen = _FakeGeneration(message=msg)
    resp = _FakeResponse(
        llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        generations=[[gen]],
    )
    inp, out = _extract(resp)
    assert inp == 10, f"llm_output should be preferred, got {inp}"
    assert out == 5, f"llm_output should be preferred, got {out}"


def test_no_token_data_returns_none():
    resp = _FakeResponse(llm_output={}, generations=[])
    inp, out = _extract(resp)
    assert inp is None, inp
    assert out is None, out


def test_malformed_usage_metadata_does_not_crash():
    """Non-dict usage_metadata must not raise."""
    msg = _FakeMessage(usage_metadata="not_a_dict")
    gen = _FakeGeneration(message=msg)
    resp = _FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp is None and out is None, (inp, out)
