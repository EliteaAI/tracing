"""
Unit tests for AuditSpanProcessor token attribute passthrough.

Run standalone:
    cd tracing && python3 -m pytest utils/tests/test_audit_processor_tokens.py -v --rootdir=utils/tests
"""

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
# Patch the relative import by pre-populating the source
import types as _t
_source = (_utils_dir / "audit_processor.py").read_text()
_source = _source.replace("from .user_context import resolve_user_email", "")
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
