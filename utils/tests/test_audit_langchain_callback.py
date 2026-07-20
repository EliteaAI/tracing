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


class FakeMessage:
    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata


class FakeGeneration:
    def __init__(self, message=None):
        self.message = message


class FakeResponse:
    def __init__(self, llm_output=None, generations=None):
        self.llm_output = llm_output
        self.generations = generations or []


from audit_langchain_callback import AuditLangChainCallback
_extract = AuditLangChainCallback._extract_tokens


def test_none_response():
    assert _extract(None) == (None, None), "None input must return (None, None)"


def test_openai_style_llm_output():
    resp = FakeResponse(llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    inp, out = _extract(resp)
    assert inp == 100, f"expected 100 input tokens, got {inp}"
    assert out == 50, f"expected 50 output tokens, got {out}"


def test_anthropic_style_llm_output():
    """Anthropic uses input_tokens / output_tokens key names."""
    resp = FakeResponse(llm_output={"token_usage": {"input_tokens": 200, "output_tokens": 80}})
    inp, out = _extract(resp)
    assert inp == 200, inp
    assert out == 80, out


def test_usage_metadata_modern():
    """Modern models expose tokens via generation.message.usage_metadata."""
    msg = FakeMessage(usage_metadata={"input_tokens": 300, "output_tokens": 120})
    gen = FakeGeneration(message=msg)
    resp = FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp == 300, inp
    assert out == 120, out


def test_usage_metadata_prompt_completion_keys():
    """Fallback key names inside usage_metadata."""
    msg = FakeMessage(usage_metadata={"prompt_tokens": 50, "completion_tokens": 20})
    gen = FakeGeneration(message=msg)
    resp = FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp == 50, inp
    assert out == 20, out


def test_llm_output_preferred_over_usage_metadata():
    """llm_output takes priority when both present."""
    msg = FakeMessage(usage_metadata={"input_tokens": 999, "output_tokens": 999})
    gen = FakeGeneration(message=msg)
    resp = FakeResponse(
        llm_output={"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        generations=[[gen]],
    )
    inp, out = _extract(resp)
    assert inp == 10, f"llm_output should be preferred, got {inp}"
    assert out == 5, f"llm_output should be preferred, got {out}"


def test_no_token_data_returns_none():
    resp = FakeResponse(llm_output={}, generations=[])
    inp, out = _extract(resp)
    assert inp is None, inp
    assert out is None, out


def test_malformed_usage_metadata_does_not_crash():
    """Non-dict usage_metadata must not raise."""
    msg = FakeMessage(usage_metadata="not_a_dict")
    gen = FakeGeneration(message=msg)
    resp = FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp is None and out is None, (inp, out)


def test_zero_value_tokens_not_treated_as_missing():
    """Zero is a valid token count — must not be confused with None/missing."""
    resp = FakeResponse(llm_output={"token_usage": {"prompt_tokens": 0, "completion_tokens": 50}})
    inp, out = _extract(resp)
    assert inp == 0, f"expected 0, got {inp}"
    assert out == 50, f"expected 50, got {out}"


def test_zero_value_via_usage_metadata():
    """Zero tokens via usage_metadata path."""
    msg = FakeMessage(usage_metadata={"input_tokens": 0, "output_tokens": 0})
    gen = FakeGeneration(message=msg)
    resp = FakeResponse(generations=[[gen]])
    inp, out = _extract(resp)
    assert inp == 0, f"expected 0, got {inp}"
    assert out == 0, f"expected 0, got {out}"


_extract_cost = AuditLangChainCallback._extract_response_cost


def test_response_cost_from_llm_output():
    resp = FakeResponse(llm_output={"response_cost": 0.0025})
    assert _extract_cost(resp) == 0.0025


def test_response_cost_from_hidden_params():
    resp = FakeResponse(llm_output={"_hidden_params": {"response_cost": 0.01}})
    assert _extract_cost(resp) == 0.01


def test_response_cost_none_when_absent():
    resp = FakeResponse(llm_output={})
    assert _extract_cost(resp) is None


def test_response_cost_zero_is_valid():
    """Zero cost is a legitimate value, not missing."""
    resp = FakeResponse(llm_output={"response_cost": 0})
    assert _extract_cost(resp) == 0.0


def test_response_cost_inf_returns_none():
    """inf must be treated as missing so the audit row is not dropped on insert."""
    resp = FakeResponse(llm_output={"response_cost": float("inf")})
    assert _extract_cost(resp) is None


def test_response_cost_nan_returns_none():
    """nan must be treated as missing so the audit row is not dropped on insert."""
    resp = FakeResponse(llm_output={"response_cost": float("nan")})
    assert _extract_cost(resp) is None


def test_response_cost_non_numeric_string_returns_none():
    resp = FakeResponse(llm_output={"response_cost": "not-a-number"})
    assert _extract_cost(resp) is None
