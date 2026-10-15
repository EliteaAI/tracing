"""One recording path for the C-1009 security counters (#6754): OTel + in-process total + log line."""
import importlib.util
import sys
import types

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from pylon.core.tools import log


@pytest.fixture
def sm(plugin_root):
    spec = importlib.util.spec_from_file_location("security_metrics_under_test",
                                                  plugin_root / "utils" / "security_metrics.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def lines(monkeypatch):
    captured = []
    monkeypatch.setattr(log, "info", lambda msg, *a, **k: captured.append(msg % a if a else msg), raising=False)
    return captured


@pytest.fixture
def otel(sm):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics = sm.SecurityMetrics(meter_getter=lambda: provider.get_meter(sm.METER_NAME))
    return types.SimpleNamespace(metrics=metrics, reader=reader)


def exported(reader):
    """``{(metric name, frozenset(attributes)): value}`` from the in-memory reader."""
    out = {}
    data = reader.get_metrics_data()
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            if scope.scope.name != "elitea.security":
                continue
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    out[(metric.name, frozenset(point.attributes.items()))] = point.value
    return out


def test_inc_updates_total_and_otel_instrument(otel, lines):
    counter = otel.metrics.counter("governed_dispatch_unavailable_total", "refused")

    counter.inc()
    counter.inc(2)

    assert counter.total() == 3
    assert exported(otel.reader) == {("governed_dispatch_unavailable_total", frozenset()): 3}


def test_labels_partition_totals_and_attributes(otel, lines):
    counter = otel.metrics.counter("worker_capability_reject_total", "rejected", ("capability",))

    counter.inc(capability="a")
    counter.inc(capability="a")
    counter.inc(capability="b")

    assert (counter.total(), counter.total(capability="a"), counter.total(capability="b")) == (3, 2, 1)
    assert counter.total(capability="never") == 0
    assert exported(otel.reader) == {
        ("worker_capability_reject_total", frozenset({("capability", "a")})): 2,
        ("worker_capability_reject_total", frozenset({("capability", "b")})): 1,
    }


def test_log_line_format_and_label_order(otel, lines):
    counter = otel.metrics.counter("m_total", "d", ("zeta", "alpha"))

    counter.inc(3, alpha="x", zeta="y")

    assert lines == ["security_metric name=m_total value=3 zeta=y alpha=x"]


def test_log_line_without_labels(otel, lines):
    otel.metrics.counter("auth_epoch_mismatch_total", "d").inc()

    assert lines == ["security_metric name=auth_epoch_mismatch_total value=1"]


def test_label_values_cannot_inject_fields_or_lines(otel, lines):
    counter = otel.metrics.counter("m_total", "d", ("capability",))

    counter.inc(capability="a value=9\nsecurity_metric name=forged")

    assert len(lines) == 1 and "\n" not in lines[0]
    assert lines[0] == "security_metric name=m_total value=1 capability=a_value_9_security_metric_name_forged"
    assert counter.total(capability="a value=9\nsecurity_metric name=forged") == 1


def test_label_value_is_bounded(sm):
    assert len(sm.label_value("x" * 500)) == sm.MAX_LABEL_VALUE_LEN
    assert sm.label_value("") == "-"


def test_wrong_labels_are_a_caller_error(otel, lines):
    counter = otel.metrics.counter("m_total", "d", ("capability",))

    with pytest.raises(ValueError):
        counter.inc()
    with pytest.raises(ValueError):
        counter.inc(capability="a", extra="b")
    assert counter.total() == 0 and lines == []


def test_counter_is_get_or_create(otel):
    first = otel.metrics.counter("m_total", "d", ("capability",))

    assert otel.metrics.counter("m_total", "d", ("capability",)) is first
    with pytest.raises(ValueError):
        otel.metrics.counter("m_total", "d", ())


def test_exporter_or_log_failure_never_breaks_the_caller(sm, monkeypatch):
    def broken_meter():
        raise RuntimeError("meter down")

    def broken_log(*a, **k):
        raise RuntimeError("log down")

    monkeypatch.setattr(log, "info", broken_log, raising=False)
    counter = sm.SecurityMetrics(meter_getter=broken_meter).counter("m_total", "d")

    counter.inc()

    assert counter.total() == 1


def test_global_meter_is_a_noop_without_a_provider(sm, lines):
    counter = sm.SecurityMetrics().counter("m_total", "d", ("capability",))

    counter.inc(capability="a")

    assert counter.total() == 1 and lines == ["security_metric name=m_total value=1 capability=a"]


def test_gauge_callback_is_observed_on_collect(otel):
    values = iter([5, 9])
    otel.metrics.gauge_callback("security_audit_outbox_depth", "depth", lambda: next(values))

    assert exported(otel.reader) == {("security_audit_outbox_depth", frozenset()): 5}
    assert exported(otel.reader) == {("security_audit_outbox_depth", frozenset()): 9}


def test_gauge_callback_skips_none_and_survives_errors(otel):
    otel.metrics.gauge_callback("lag_none", "d", lambda: None)

    def boom():
        raise RuntimeError("db down")
    otel.metrics.gauge_callback("lag_boom", "d", boom)

    assert exported(otel.reader) == {}


def test_gauge_registration_is_idempotent(otel):
    otel.metrics.gauge_callback("g", "d", lambda: 1)
    otel.metrics.gauge_callback("g", "d", lambda: 2)

    assert exported(otel.reader) == {("g", frozenset()): 1}


def test_module_registers_the_tool_before_the_enabled_gate_and_unregisters_it(plugin_root):
    source = (plugin_root / "module.py").read_text()
    init = source[source.index("    def init(self):"):source.index("    def ready(self):")]

    assert init.index('self.descriptor.register_tool("security_metrics", SECURITY_METRICS)') \
        < init.index("self._init_security_audit()") < init.index("if not self._enabled:")
    assert 'self.descriptor.unregister_tool("security_metrics")' in source[source.index("    def deinit(self):"):]
