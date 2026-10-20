"""Request-body capture is skipped on write-only secret paths (#7120, C-1009).

The personal credential value is PUT to ``/api/v2/secrets/credential_material/...``. Key masking is
not enough for it (a field named "account" or "value" is not masked), so the body is never read
for tracing: no span attribute and no log line may contain any of it.
"""
import importlib
import json
import sys
import types

import pytest
from flask import Flask, request
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SECRET_PATH = "/api/v2/secrets/credential_material/administration/1/7e0f6c1a-5d2b-4c53-9f77-0d2f6d6f9a10"
OTHER_PATH = "/api/v2/elitea_core/predict/prompt_lib/1/5"
# Field names that no masking rule matches: only the skip can keep these out.
SECRET_BODY = {"fields": {"account": "zq-personal-value-7731", "username": "roman-private-login"}}
MARKERS = ("zq-personal-value-7731", "roman-private-login")


class LogRecorder:
    def __init__(self):
        self.lines = []

    def _record(self, message, *args, **kwargs):
        self.lines.append(str(message) % args if args else str(message))

    debug = info = warning = warn = error = exception = critical = trace = _record


@pytest.fixture
def trace_pkg(monkeypatch, plugin_root):
    root = "tracing_pkg_7120_capture"
    for name, sub in ((root, ""), (f"{root}.core", "core"), (f"{root}.middleware", "middleware"),
                      (f"{root}.utils", "utils")):
        package = types.ModuleType(name)
        package.__path__ = [str(plugin_root / sub) if sub else str(plugin_root)]
        monkeypatch.setitem(sys.modules, name, package)
    ns = types.SimpleNamespace(
        flask_trace=importlib.import_module(f"{root}.middleware.flask_trace"),
        capture=importlib.import_module(f"{root}.utils.payload_capture"),
    )
    yield ns
    for name in [n for n in sys.modules if n.startswith(root)]:
        sys.modules.pop(name, None)


@pytest.fixture
def traced(trace_pkg, monkeypatch):
    """A Flask app with the real tracing hooks; yields (client, spans, logs, install(payload_config))."""
    logs = LogRecorder()
    monkeypatch.setattr(trace_pkg.flask_trace, "log", logs)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    seen = []
    app = Flask(__name__)

    def echo(**kwargs):
        seen.append(request.get_json(silent=True))
        return {"ok": True}

    for path in (SECRET_PATH, OTHER_PATH, "/custom/area/x", "/api/v2/secrets/credential_material_other/x"):
        app.add_url_rule(path, path, echo, methods=["PUT", "POST", "PATCH"])
    state = types.SimpleNamespace(app=app, spans=exporter, logs=logs, handler_bodies=seen)

    def install(payload_config=None):
        trace_pkg.flask_trace.FlaskTraceHooks(
            app, provider.get_tracer("t"), capture_payload=True, payload_config=payload_config,
            capture_user_context=False).install()
        return app.test_client()

    state.install = install
    return state


def attributes(state):
    return [dict(span.attributes) for span in state.spans.get_finished_spans()]


def assert_no_marker(state, caplog):
    haystack = json.dumps(attributes(state), default=str) + "\n".join(state.logs.lines) + caplog.text
    for marker in MARKERS:
        assert marker not in haystack


@pytest.mark.parametrize("method", ["PUT", "POST", "PATCH"])
def test_body_on_the_credential_material_path_is_never_captured(traced, caplog, method):
    caplog.set_level("DEBUG")
    client = traced.install()

    response = client.open(SECRET_PATH, method=method, json=SECRET_BODY)

    assert response.status_code == 200
    spans = attributes(traced)
    assert len(spans) == 1 and "http.request.body" not in spans[0]
    assert_no_marker(traced, caplog)


def test_the_handler_still_receives_the_body(traced):
    client = traced.install()

    client.put(SECRET_PATH, json=SECRET_BODY)

    assert traced.handler_bodies == [SECRET_BODY]


def test_query_string_does_not_change_the_decision(traced, caplog):
    caplog.set_level("DEBUG")
    client = traced.install()

    client.put(f"{SECRET_PATH}?expected_generation=1&x=/api/v2/other", json=SECRET_BODY)

    assert "http.request.body" not in attributes(traced)[0]
    assert_no_marker(traced, caplog)


def test_other_paths_are_still_captured(traced):
    client = traced.install()

    client.put(OTHER_PATH, json=SECRET_BODY)

    body = attributes(traced)[0]["http.request.body"]
    assert all(marker in body for marker in MARKERS)


def test_a_path_that_only_looks_similar_is_still_captured(traced):
    client = traced.install()

    client.put("/api/v2/secrets/credential_material_other/x", json=SECRET_BODY)

    assert "http.request.body" in attributes(traced)[0]


def test_other_capture_on_the_skipped_path_is_unchanged(traced):
    client = traced.install()

    client.put(f"{SECRET_PATH}?page=2", json=SECRET_BODY, headers={"Content-Type": "application/json"})

    span = attributes(traced)[0]
    assert span["http.target"] == SECRET_PATH and "page" in span["http.request.query"]
    assert "content-type" in span["http.request.headers"].lower()


@pytest.mark.parametrize("config", [None, {}, {"enabled": True, "max_size": 100}, {"skip_body_paths": []}])
def test_the_default_applies_whatever_the_config_says(traced, caplog, config):
    caplog.set_level("DEBUG")
    client = traced.install(config)

    client.put(SECRET_PATH, json=SECRET_BODY)

    assert "http.request.body" not in attributes(traced)[0]
    assert_no_marker(traced, caplog)


def test_configured_paths_extend_the_default(traced, caplog):
    caplog.set_level("DEBUG")
    client = traced.install({"skip_body_paths": ["/custom/area/"]})

    client.put("/custom/area/x", json=SECRET_BODY)
    client.put(SECRET_PATH, json=SECRET_BODY)
    client.put(OTHER_PATH, json=SECRET_BODY)

    spans = attributes(traced)
    assert [("http.request.body" in s) for s in spans] == [False, False, True]


# --- PayloadCapture.skips_body ---------------------------------------------------------------

@pytest.mark.parametrize("path,skipped", [
    (SECRET_PATH, True),
    (SECRET_PATH + "?a=1", True),
    ("/api/v2/secrets/credential_material/", True),
    ("//api/v2/secrets/credential_material/administration/1/x", True),
    ("/api/v2/secrets/credential_material", False),
    ("/api/v2/secrets/credential_references/administration/1", False),
    ("/api/v2/other?next=/api/v2/secrets/credential_material/", False),
    ("", False),
    (None, False),
])
def test_skips_body_is_a_path_prefix_match_ignoring_the_query(trace_pkg, path, skipped):
    assert trace_pkg.capture.PayloadCapture().skips_body(path) is skipped


def test_get_payload_capture_carries_the_setting(trace_pkg):
    capture = trace_pkg.capture.get_payload_capture({"skip_body_paths": ["/x/"]})

    assert capture.skips_body("/x/1") and capture.skips_body(SECRET_PATH)
    assert trace_pkg.capture.get_payload_capture().skips_body(SECRET_PATH)


def test_serialization_of_other_data_is_unchanged(trace_pkg):
    capture = trace_pkg.capture.PayloadCapture()

    out = capture.serialize_http_request(body={"prompt": "hi", "api_key": "k"})

    assert json.loads(out["http.request.body"]) == {"prompt": "hi", "api_key": "***MASKED***"}
