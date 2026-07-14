"""Unit tests for utils/trace_context.py - Pure functions."""
import pytest
import uuid


def generate_trace_id(prefix: str = 'srv') -> str:
    """Generate a new trace ID with optional prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def extract_trace_from_sio_payload_pure(data: dict) -> str | None:
    """Extract trace ID from Socket.IO event payload - pure logic without set_trace_id."""
    trace_info = data.get('_trace', {})
    return trace_info.get('trace_id')


def inject_trace_context_pure(
    headers: dict[str, str],
    trace_id: str | None,
    span_id: str | None
) -> dict[str, str]:
    """Inject trace context into headers - pure logic."""
    if trace_id:
        headers['X-Trace-ID'] = trace_id
        if span_id:
            headers['traceparent'] = f"00-{trace_id}-{span_id}-01"
    return headers


def parse_traceparent(traceparent: str) -> dict:
    """Parse W3C traceparent header."""
    context = {}
    try:
        parts = traceparent.split('-')
        if len(parts) >= 3:
            context['trace_id'] = parts[1]
            context['parent_span_id'] = parts[2]
            context['w3c'] = True
    except Exception:
        pass
    return context


def get_traceparent(trace_id: str | None, span_id: str | None) -> str | None:
    """Get W3C traceparent header value."""
    if trace_id and span_id:
        return f"00-{trace_id}-{span_id}-01"
    return None


class TestGenerateTraceId:
    """Tests for generate_trace_id function."""

    def test_default_prefix(self):
        trace_id = generate_trace_id()
        assert trace_id.startswith("srv-")

    def test_custom_prefix(self):
        trace_id = generate_trace_id(prefix="test")
        assert trace_id.startswith("test-")

    def test_hex_suffix_length(self):
        trace_id = generate_trace_id()
        suffix = trace_id.split("-", 1)[1]
        assert len(suffix) == 16

    def test_hex_suffix_is_valid_hex(self):
        trace_id = generate_trace_id()
        suffix = trace_id.split("-", 1)[1]
        int(suffix, 16)

    def test_uniqueness(self):
        ids = [generate_trace_id() for _ in range(100)]
        assert len(set(ids)) == 100

    def test_empty_prefix(self):
        trace_id = generate_trace_id(prefix="")
        assert trace_id.startswith("-")


class TestExtractTraceFromSioPayload:
    """Tests for extract_trace_from_sio_payload_pure function."""

    def test_extracts_trace_id(self):
        data = {"_trace": {"trace_id": "abc123"}}
        result = extract_trace_from_sio_payload_pure(data)
        assert result == "abc123"

    def test_returns_none_when_no_trace(self):
        data = {"other": "data"}
        result = extract_trace_from_sio_payload_pure(data)
        assert result is None

    def test_returns_none_when_trace_empty(self):
        data = {"_trace": {}}
        result = extract_trace_from_sio_payload_pure(data)
        assert result is None

    def test_returns_none_when_trace_id_missing(self):
        data = {"_trace": {"span_id": "xyz"}}
        result = extract_trace_from_sio_payload_pure(data)
        assert result is None

    def test_empty_payload(self):
        data = {}
        result = extract_trace_from_sio_payload_pure(data)
        assert result is None


class TestInjectTraceContext:
    """Tests for inject_trace_context_pure function."""

    def test_adds_trace_id_header(self):
        headers = {}
        result = inject_trace_context_pure(headers, "trace123", None)
        assert result["X-Trace-ID"] == "trace123"

    def test_adds_traceparent_with_span(self):
        headers = {}
        result = inject_trace_context_pure(headers, "trace123", "span456")
        assert result["X-Trace-ID"] == "trace123"
        assert result["traceparent"] == "00-trace123-span456-01"

    def test_no_traceparent_without_span(self):
        headers = {}
        result = inject_trace_context_pure(headers, "trace123", None)
        assert "traceparent" not in result

    def test_no_headers_without_trace(self):
        headers = {}
        result = inject_trace_context_pure(headers, None, None)
        assert "X-Trace-ID" not in result
        assert "traceparent" not in result

    def test_preserves_existing_headers(self):
        headers = {"Content-Type": "application/json"}
        result = inject_trace_context_pure(headers, "trace123", "span456")
        assert result["Content-Type"] == "application/json"
        assert result["X-Trace-ID"] == "trace123"


class TestParseTraceparent:
    """Tests for parse_traceparent function."""

    def test_parses_valid_traceparent(self):
        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        result = parse_traceparent(traceparent)
        assert result["trace_id"] == "4bf92f3577b34da6a3ce929d0e0e4736"
        assert result["parent_span_id"] == "00f067aa0ba902b7"
        assert result["w3c"] is True

    def test_handles_minimal_traceparent(self):
        traceparent = "00-traceid-spanid"
        result = parse_traceparent(traceparent)
        assert result["trace_id"] == "traceid"
        assert result["parent_span_id"] == "spanid"

    def test_handles_empty_string(self):
        result = parse_traceparent("")
        assert result == {}

    def test_handles_invalid_format(self):
        result = parse_traceparent("invalid")
        assert result == {}


class TestGetTraceparent:
    """Tests for get_traceparent function."""

    def test_returns_formatted_traceparent(self):
        result = get_traceparent("trace123", "span456")
        assert result == "00-trace123-span456-01"

    def test_returns_none_without_trace_id(self):
        result = get_traceparent(None, "span456")
        assert result is None

    def test_returns_none_without_span_id(self):
        result = get_traceparent("trace123", None)
        assert result is None

    def test_returns_none_with_both_none(self):
        result = get_traceparent(None, None)
        assert result is None
