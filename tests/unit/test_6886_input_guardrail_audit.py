"""Input guardrail (#6886): the value redactor hook and the guardrail audit row.

Loads the real modules by path so the tests track the shipped code, with only
``pylon.core.tools.log`` and ``user_context`` stubbed.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

UTILS = pathlib.Path(__file__).resolve().parents[2] / "utils"
FAKE_SECRET = "ghp_" + "Q7wZ" * 9  # generated, matches the GitHub PAT shape only
REDACTED = "[REDACTED:credentials]"


def _load(name, monkeypatch, extra=None):
    pkg = types.ModuleType("_tracing_utils")
    pkg.__path__ = [str(UTILS)]
    log = types.SimpleNamespace(debug=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "_tracing_utils", pkg)
    monkeypatch.setitem(sys.modules, "pylon", types.ModuleType("pylon"))
    monkeypatch.setitem(sys.modules, "pylon.core", types.ModuleType("pylon.core"))
    monkeypatch.setitem(sys.modules, "pylon.core.tools", types.SimpleNamespace(log=log))
    for mod_name, mod in (extra or {}).items():
        monkeypatch.setitem(sys.modules, f"_tracing_utils.{mod_name}", mod)
    spec = importlib.util.spec_from_file_location(f"_tracing_utils.{name}", UTILS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def capture_mod(monkeypatch):
    module = _load("payload_capture", monkeypatch)
    yield module
    module.register_value_redactor(None)


def _redactor(text):
    return text.replace(FAKE_SECRET, REDACTED)


class TestValueRedactor:
    def test_without_redactor_the_whole_value_is_masked(self, capture_mod):
        out = capture_mod.PayloadCapture().serialize({"user_input": f"deploy with {FAKE_SECRET} now"})

        assert FAKE_SECRET not in out
        assert capture_mod.MASK_VALUE in out

    def test_redactor_keeps_the_readable_part(self, capture_mod):
        capture_mod.register_value_redactor(_redactor)

        out = capture_mod.PayloadCapture().serialize({"user_input": f"deploy with {FAKE_SECRET} now"})

        assert FAKE_SECRET not in out
        assert f"deploy with {REDACTED} now" in out

    def test_failing_redactor_falls_back_to_the_mask(self, capture_mod):
        def broken(_text):
            raise RuntimeError("boom")

        capture_mod.register_value_redactor(broken)

        out = capture_mod.PayloadCapture().serialize({"user_input": f"deploy with {FAKE_SECRET}"})

        assert FAKE_SECRET not in out
        assert capture_mod.MASK_VALUE in out

    def test_pattern_mask_still_applies_after_redaction(self, capture_mod):
        capture_mod.register_value_redactor(lambda text: text)  # detects nothing

        out = capture_mod.PayloadCapture().serialize({"user_input": f"x {FAKE_SECRET}"})

        assert FAKE_SECRET not in out

    def test_clean_text_is_unchanged(self, capture_mod):
        capture_mod.register_value_redactor(_redactor)

        out = capture_mod.PayloadCapture().serialize({"user_input": "hello"})

        assert '"hello"' in out


class TestGuardrailAuditRow:
    @pytest.fixture
    def processor(self, monkeypatch):
        user_context = types.SimpleNamespace(resolve_user_email=lambda user_id: "user@example.com")
        module = _load("audit_processor", monkeypatch, {"user_context": user_context})
        return module.AuditSpanProcessor(lambda data: None)

    def _snap(self, attrs):
        return {
            "name": attrs.get("audit.guardrail.action", "span"),
            "attrs": attrs,
            "trace_id": 0x1234,
            "span_id": 0x56,
            "parent_span_id": 0x78,
            "start_time": 1_000_000_000,
            "end_time": 1_000_000_000,
            "status_ok": True,
        }

    def test_guardrail_span_becomes_a_guardrail_row(self, processor):
        event = processor._extract(self._snap({
            "audit.observation.type": "guardrail",
            "audit.guardrail.action": "input:credentials.github_token:block",
            "audit.tool.name": "user_input",
            "audit.is_error": False,
            "user.id": 7,
            "project.id": 2,
        }))

        assert event["event_type"] == "guardrail"
        assert event["action"] == "input:credentials.github_token:block"
        assert event["tool_name"] == "user_input"
        assert event["user_id"] == 7 and event["project_id"] == 2
        assert event["user_email"] == "user@example.com"
        assert event["is_error"] is False
        assert len(event["event_type"]) <= 32

    def test_error_rows_are_flagged(self, processor):
        event = processor._extract(self._snap({
            "audit.observation.type": "guardrail",
            "audit.guardrail.action": "input:error.timeout:fail_open",
            "audit.is_error": True,
        }))

        assert event["is_error"] is True
        assert "tool_name" not in event

    def test_guardrail_span_passes_the_on_end_filter(self, processor):
        written = []
        processor.write_fn = written.append
        span = types.SimpleNamespace(
            name="input:credentials.github_token:warn",
            attributes={"audit.observation.type": "guardrail",
                        "audit.guardrail.action": "input:credentials.github_token:warn"},
            get_span_context=lambda: types.SimpleNamespace(trace_id=1, span_id=2),
            parent=None, start_time=1, end_time=2,
            status=types.SimpleNamespace(is_ok=True),
        )

        processor.on_end(span)
        processor.force_flush(timeout_millis=2000)

        assert [w["event_type"] for w in written] == ["guardrail"]
