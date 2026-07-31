"""
Unit tests for AuditSpanProcessor token attribute passthrough.

Run standalone:
    cd tracing && python3 -m pytest utils/tests/test_audit_processor_tokens.py -v --rootdir=utils/tests
"""

import json
import sys
import types
import pathlib
import unittest.mock as mock

# Stub pylon.core.tools before import
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

# Stub user_context (both as standalone and as relative import target)
_user_context = types.ModuleType("user_context")
_user_context.resolve_user_email = lambda user_id: None
sys.modules["user_context"] = _user_context

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Patch the relative import: audit_processor lives in a package that uses
# "from .user_context import ...", so we import via importlib with proper setup
import importlib
import importlib.util

_utils_dir = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "audit_processor",
    str(_utils_dir / "audit_processor.py"),
    submodule_search_locations=[],
)
_mod = importlib.util.module_from_spec(_spec)
# Inject resolve_user_email directly before exec
_mod.resolve_user_email = lambda user_id: None
# Load the real model_pricing module (from the tracing utils directory) and
# inject its compute_llm_cost so the relative import inside _extract_llm
# resolves without needing the tracing package on sys.path.
_mp_spec = importlib.util.spec_from_file_location(
    "model_pricing", str(_utils_dir / "model_pricing.py"),
)
_mp_mod = importlib.util.module_from_spec(_mp_spec)
_mp_spec.loader.exec_module(_mp_mod)
sys.modules["model_pricing"] = _mp_mod

# Patch the relative imports by rewriting the source before compile+exec:
# audit_processor.py uses "from .user_context import ..." (module-scope) and
# "from .model_pricing import ..." (inside _extract_llm). We turn the second
# into an absolute import against our sys.modules stub.
import types as _t
_source = (_utils_dir / "audit_processor.py").read_text()
_source = _source.replace("from .user_context import resolve_user_email", "")
_source = _source.replace(
    "from .model_pricing import compute_llm_cost",
    "from model_pricing import compute_llm_cost",
)
exec(compile(_source, str(_utils_dir / "audit_processor.py"), "exec"), _mod.__dict__)
sys.modules["audit_processor"] = _mod

from audit_processor import AuditSpanProcessor


def _make_snap(attrs):
    return {
        "name": "gpt-4o",
        "attrs": attrs,
        "trace_id": 0,
        "span_id": 0,
        "parent_span_id": None,
        "start_time": 1_000_000_000,
        "end_time": 2_000_000_000,
        "status_ok": True,
    }


def _make_processor():
    events = []
    proc = AuditSpanProcessor(write_fn=events.append)
    return proc, events


def test_extract_llm_no_tokens():
    """When token attributes absent, event has no token keys."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event is not None
    assert "input_tokens" not in event, event
    assert "output_tokens" not in event, event


def test_extract_llm_with_tokens():
    """When token attributes present, event includes them."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
        "audit.input_tokens": 150,
        "audit.output_tokens": 60,
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event is not None
    assert event.get("input_tokens") == 150, event
    assert event.get("output_tokens") == 60, event


def test_extract_llm_tokens_coerced_to_int():
    """String values from span attributes are coerced to int."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
        "audit.input_tokens": "200",
        "audit.output_tokens": "75",
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 200, event
    assert event.get("output_tokens") == 75, event


def test_build_event_llm_cost_passthrough():
    """llm_cost passed as kwarg appears in the returned event dict."""
    proc, _ = _make_processor()
    snap = _make_snap({"audit.observation.type": "generation"})
    event = proc._build_event(
        snap=snap,
        attrs=snap["attrs"],
        event_type="llm",
        action="gpt-4o",
        model_name="gpt-4o",
        input_tokens=100,
        output_tokens=40,
        llm_cost=0.00123456,
        is_error=False,
    )
    assert event.get("input_tokens") == 100, event
    assert event.get("output_tokens") == 40, event
    assert abs(event.get("llm_cost", 0) - 0.00123456) < 1e-10, event


def test_extract_llm_non_numeric_string_graceful():
    """Non-numeric string attributes should not crash, tokens become None."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
        "audit.input_tokens": "N/A",
        "audit.output_tokens": "unknown",
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event is not None
    assert event.get("input_tokens") is None, event
    assert event.get("output_tokens") is None, event


# --- Langfuse-namespace reader (ADR-0008 Phase A) ---


def test_langfuse_normalized_shape():
    """Anthropic/Vertex/Bedrock/Ollama shape after Langfuse normalization."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "claude-3-5-sonnet",
        "langfuse.observation.usage_details": json.dumps(
            {"input": 150, "output": 60, "total": 210}
        ),
        "langfuse.observation.cost_details": json.dumps({"total": 0.00123456}),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 150, event
    assert event.get("output_tokens") == 60, event
    assert abs(event.get("llm_cost", 0) - 0.00123456) < 1e-10, event
    assert event.get("token_source") == "langfuse", event
    assert event.get("cost_source") == "observed", event


def test_langfuse_openai_bypass_shape():
    """OpenAI/LiteLLM bypass shape — prompt_tokens/completion_tokens preserved."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "gpt-4o",
        "langfuse.observation.usage_details": json.dumps({
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 200, event
    assert event.get("output_tokens") == 80, event
    assert event.get("token_source") == "langfuse", event
    # No cost_details on span, but pricing table kicks in for gpt-4o.
    expected = 200 * 2.5e-6 + 80 * 1.0e-5
    assert abs(event.get("llm_cost", 0) - expected) < 1e-10, event
    assert event.get("cost_source", "").startswith("estimated:"), event


def test_langfuse_only_no_audit_attrs():
    """Regression: real production shape when Langfuse is configured.

    audit.* keys are entirely absent — only langfuse.observation.* present.
    """
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "gpt-4o-mini",
        "langfuse.observation.usage_details": json.dumps({
            "input": 42, "output": 8, "total": 50,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 42, event
    assert event.get("output_tokens") == 8, event
    assert event.get("token_source") == "langfuse", event


def test_langfuse_malformed_json_falls_back_to_audit():
    """Broken JSON on the Langfuse attr must not crash — fall back to audit.*."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
        "audit.input_tokens": 150,
        "audit.output_tokens": 60,
        # Not valid JSON — parser must return None and skip this namespace.
        "langfuse.observation.usage_details": "this is not json {{{",
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 150, event
    assert event.get("output_tokens") == 60, event
    assert event.get("token_source") == "audit", event


def test_langfuse_wins_over_audit_when_both_present():
    """When both namespaces are populated, Langfuse takes precedence."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.usage_details": json.dumps(
            {"input": 999, "output": 888}
        ),
        "audit.input_tokens": 111,
        "audit.output_tokens": 222,
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("input_tokens") == 999, event
    assert event.get("output_tokens") == 888, event
    assert event.get("token_source") == "langfuse", event


def test_langfuse_cost_details_breakdown_allowlist():
    """cost_details without a total: sum only known keys; unknown keys dropped."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.usage_details": json.dumps({"input": 100, "output": 50}),
        "langfuse.observation.cost_details": json.dumps({
            "input": 0.001,
            "output": 0.002,
            "unknown_future_key": 999.0,  # must be ignored
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert abs(event.get("llm_cost", 0) - 0.003) < 1e-10, event
    assert event.get("cost_source") == "observed", event


def test_langfuse_cost_details_total_null_falls_through_to_breakdown():
    """Real Langfuse payloads sometimes carry {"total": null, "input": ..., "output": ...}.

    Prior behavior treated the null as a parse failure and returned None,
    triggering a spurious cost estimate. The breakdown must be used instead.
    """
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.usage_details": json.dumps({"input": 100, "output": 50}),
        "langfuse.observation.cost_details": json.dumps({
            "total": None,
            "input": 0.001,
            "output": 0.002,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert abs(event.get("llm_cost", 0) - 0.003) < 1e-10, event
    assert event.get("cost_source") == "observed", event


# --- Cost estimation from vendored LiteLLM pricing (ADR-0008 Phase B) ---


def test_cost_estimated_gpt4o():
    """gpt-4o with known token counts: cost computed from pricing table."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "gpt-4o",
        "langfuse.observation.usage_details": json.dumps({
            "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
        }),
        # No cost_details on the span — must fall through to estimation.
    })
    event = proc._extract_llm(snap, snap["attrs"])
    # gpt-4o v1.83.14: input $2.5e-6/token, output $1.0e-5/token
    expected = 1000 * 2.5e-6 + 500 * 1.0e-5
    assert abs(event.get("llm_cost", 0) - expected) < 1e-10, event
    assert event.get("cost_source", "").startswith("estimated:litellm-"), event


def test_cost_estimated_claude_with_cache_read():
    """Anthropic claude cache-read tokens priced at their own rate."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "langfuse.observation.usage_details": json.dumps({
            # Langfuse normalized shape decrements cache tokens from input.
            "input": 800,
            "output": 200,
            "input_cache_read_input_tokens": 200,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    # us.anthropic.claude-3-5-sonnet-20241022-v2:0 v1.83.14-stable:
    #   input 3e-6, output 1.5e-5, cache_read_input_token_cost 3e-7
    # Langfuse's "input"=800 is already net-of-cache; compute_llm_cost takes
    # it as-is, then adds cache_read_input_tokens separately.
    expected = 800 * 3e-6 + 200 * 1.5e-5 + 200 * 3e-7
    assert abs(event.get("llm_cost", 0) - expected) < 1e-10, event
    assert event.get("cost_source", "").startswith("estimated:"), event


def test_cost_bedrock_prefix_stripped():
    """Bedrock inference-profile IDs (with us./eu./ prefixes) resolve to a price."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "langfuse.observation.usage_details": json.dumps({
            "input": 1000, "output": 500,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("llm_cost") is not None, event
    assert event.get("cost_source", "").startswith("estimated:"), event


def test_cost_unknown_model_stays_null():
    """Nonsense model name: llm_cost/cost_source both remain None."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "definitely-not-a-real-model-xyz-999",
        "langfuse.observation.usage_details": json.dumps({
            "input": 100, "output": 50,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert event.get("llm_cost") is None, event
    assert event.get("cost_source") is None, event


def test_cost_observed_wins_over_estimated():
    """When audit.llm_cost is set, cost_source stays 'observed'."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "audit.observation.type": "generation",
        "audit.model.name": "gpt-4o",
        "audit.input_tokens": 1000,
        "audit.output_tokens": 500,
        "audit.llm_cost": 0.00099,  # explicit observed cost
    })
    event = proc._extract_llm(snap, snap["attrs"])
    assert abs(event.get("llm_cost", 0) - 0.00099) < 1e-10, event
    assert event.get("cost_source") == "observed", event


def test_openai_bypass_cache_tokens_priced_correctly_but_input_preserved():
    """OpenAI-bypass shape: prompt_tokens is inclusive of cached tokens.

    Two things must hold:
    - Persisted input_tokens equals RAW prompt_tokens (1000), so the
      analytics UI shows what the user actually sent.
    - llm_cost is estimated on the NET-of-cache count so cached tokens
      aren't double-charged (base rate + cache_read rate).
    """
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "gpt-4o",
        # OpenAI shape: prompt_tokens=1000 INCLUDES 500 cached tokens
        "langfuse.observation.usage_details": json.dumps({
            "prompt_tokens": 1000,
            "completion_tokens": 500,
            "total_tokens": 1500,
            "input_cached_tokens": 500,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    # PERSISTED value: raw prompt_tokens count, not net-of-cache.
    assert event.get("input_tokens") == 1000, event
    assert event.get("output_tokens") == 500, event
    # COST: computed on 500 net-of-cache input + 500 cache_read at
    # gpt-4o's cache_read rate, plus 500 output.
    # gpt-4o v1.83.14-stable: input 2.5e-6, output 1e-5, cache_read 1.25e-6
    expected = 500 * 2.5e-6 + 500 * 1e-5 + 500 * 1.25e-6
    assert abs(event.get("llm_cost", 0) - expected) < 1e-10, event


def test_langfuse_normalized_shape_no_cache_double_subtract():
    """Anthropic-normalized shape: 'input' is already net-of-cache — do NOT subtract again."""
    proc, _ = _make_processor()
    snap = _make_snap({
        "langfuse.observation.type": "generation",
        "langfuse.observation.model.name": "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
        "langfuse.observation.usage_details": json.dumps({
            # Anthropic shape: "input"=800 is ALREADY net of the 200 cache_read
            "input": 800,
            "output": 200,
            "input_cache_read_input_tokens": 200,
        }),
    })
    event = proc._extract_llm(snap, snap["attrs"])
    # v1.83.14-stable: input 3e-6, output 1.5e-5, cache_read 3e-7
    expected = 800 * 3e-6 + 200 * 1.5e-5 + 200 * 3e-7
    assert abs(event.get("llm_cost", 0) - expected) < 1e-10, event
