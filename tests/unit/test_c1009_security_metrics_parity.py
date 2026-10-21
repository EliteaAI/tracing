"""Parity of the two accessor implementations (C-1009, #6754).

The vectors below are the parity contract: they run against the live ``utils/security_metrics.py`` classes and
against ``tests/reference/security_metrics_client.py`` (the template of the consumer copies in auth, indexer_worker
and elitea_core), so the two cannot drift apart silently. Change a vector only together with both implementations.
"""
import importlib.util
import sys
import types

import pytest
from pylon.core.tools import log

REFERENCE = "reference"
LIVE = "live"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=[LIVE, REFERENCE])
def impl(request, plugin_root, monkeypatch):
    """``make(name, labels)`` plus the captured ``lines``, the fake ``clock`` and the armed ``timers``."""
    lines, timers, clock = [], [], Clock()
    monkeypatch.setattr(log, "info", lambda msg, *a: lines.append(msg % a if a else msg), raising=False)
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))  # no tracing tool: the local path
    schedule = lambda delay, fn: timers.append((delay, fn))  # noqa: E731
    if request.param == LIVE:
        module = _load("parity_live", plugin_root / "utils" / "security_metrics.py")
        metrics = module.SecurityMetrics(meter_getter=lambda: types.SimpleNamespace(
            create_counter=lambda *a, **k: types.SimpleNamespace(add=lambda *x: None)), clock=clock, scheduler=schedule)
        make = lambda name, labels=(): metrics.counter(name, "d", labels)  # noqa: E731
    else:
        module = _load("parity_reference", plugin_root / "tests" / "reference" / "security_metrics_client.py")
        module._clock, module.schedule_flush = clock, schedule
        make = lambda name, labels=(): module.SecurityCounter(name, "d", labels)  # noqa: E731
    sys.modules.pop(module.__name__, None)
    return types.SimpleNamespace(make=make, lines=lines, clock=clock, timers=timers, module=module)


def test_constants(impl):
    assert (impl.module.ALLOWED_LABEL_KEYS, impl.module.MAX_LABEL_VALUE_LEN, impl.module.LOG_INTERVAL_S) == (
        frozenset({"capability"}), 64, 10.0)


def test_line_format_token_rules_and_rate_cap(impl):
    plain = impl.make("auth_epoch_mismatch_total")
    labelled = impl.make("worker_capability_reject_total", ("capability",))

    plain.inc()
    labelled.inc(capability="task_envelope.v1")
    labelled.inc(capability="x y=1\nz")
    plain.inc()
    plain.inc()
    impl.clock.t += 10
    plain.inc()
    labelled.inc(capability="")
    labelled.inc(capability="é" * 100)

    assert impl.lines == [
        "security_metric name=auth_epoch_mismatch_total value=1",
        "security_metric name=worker_capability_reject_total value=1 capability=task_envelope.v1",
        "security_metric name=worker_capability_reject_total value=1 capability=x_y_1_z",
        "security_metric name=auth_epoch_mismatch_total value=3",
        "security_metric name=worker_capability_reject_total value=1 capability=-",
        "security_metric name=worker_capability_reject_total value=1 capability=" + "_" * 64,
    ]
    assert plain.total() == 4 and labelled.total() == 4


def test_a_burst_that_stops_inside_the_window_is_flushed_once_at_its_end(impl):
    counter = impl.make("c_total", ("capability",))

    counter.inc(capability="a")
    impl.clock.t += 3
    counter.inc(capability="a")
    counter.inc(2, capability="a")

    assert impl.lines == ["security_metric name=c_total value=1 capability=a"]
    assert [round(delay, 6) for delay, _ in impl.timers] == [7.0]
    impl.clock.t += 7
    impl.timers[0][1]()
    impl.timers[0][1]()
    assert impl.lines[1:] == ["security_metric name=c_total value=3 capability=a"]


def test_a_flush_that_wakes_early_waits_out_the_window(impl):
    counter = impl.make("c_total")

    counter.inc()
    counter.inc()
    impl.clock.t += 10
    counter.inc()
    counter.inc()
    impl.clock.t += 1
    impl.timers[0][1]()
    assert [round(delay, 6) for delay, _ in impl.timers] == [10.0, 9.0]
    impl.clock.t += 9
    impl.timers[1][1]()

    assert impl.lines == ["security_metric name=c_total value=1", "security_metric name=c_total value=2",
                          "security_metric name=c_total value=1"]


@pytest.mark.parametrize("value", [0, -1, 1.5, True, None, "2"])
def test_only_positive_integers_are_counted(impl, value):
    counter = impl.make("c_total")

    counter.inc(value)

    assert counter.total() == 0 and impl.lines == [] and impl.timers == []


@pytest.mark.parametrize("key", ["user_id", "project_id", "token", "reason"])
def test_label_keys_outside_the_allow_list_are_refused(impl, key):
    with pytest.raises(ValueError):
        impl.make("m_total", (key,))


def test_wrong_labels_are_a_caller_error(impl):
    counter = impl.make("m_total", ("capability",))

    with pytest.raises(ValueError):
        counter.inc()
    with pytest.raises(ValueError):
        counter.inc(capability="a", extra="b")
    assert counter.total() == 0 and impl.lines == []


def test_partial_totals_match_label_sets(impl):
    counter = impl.make("m_total", ("capability",))

    counter.inc(capability="a")
    counter.inc(capability="b")
    counter.inc(capability="a")

    assert (counter.total(), counter.total(capability="a"), counter.total(capability="b"),
            counter.total(capability="never")) == (3, 2, 1, 0)
