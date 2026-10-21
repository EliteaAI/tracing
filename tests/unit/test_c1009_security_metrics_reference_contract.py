"""Contract of the security metrics accessor copy in this plugin (C-1009, #6754).

``tests/reference/security_metrics_client.py`` (the template of the consumer copies) duplicates the accessor of the tracing plugin. The values below are the contract: the same vectors are
pinned in tracing, auth, indexer_worker and elitea_core, so a change to the line format, the token rules, the label
allow-list, the rate cap or the source itself has to be made in all four places.
"""

import pathlib
import hashlib
import importlib.util
import sys
import types

import pytest

CLIENT_PATH = pathlib.Path(__file__).resolve().parents[1] / "reference" / "security_metrics_client.py"

# Pins the source of the accessor copy: change the reference in tracing (tests/reference/security_metrics_client.py),
# copy it over the three consumer copies, then paste `python -c "import hashlib,sys; print(hashlib.sha256(
# open(sys.argv[1],'rb').read().replace(b'\r\n',b'\n')).hexdigest())" <file>` into EXPECTED_SHA256 in all four repos.
EXPECTED_SHA256 = "ec2a894e3ea6e2bc69721ba236aa88b58f4df021833a374a0c4f9915d8029ba3"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def client(monkeypatch):
    """The accessor in its local-fallback mode: no tracing tool, lines captured from pylon's log, timers faked."""
    lines, timers = [], []
    monkeypatch.setitem(sys.modules, "pylon", types.ModuleType("pylon"))
    monkeypatch.setitem(sys.modules, "pylon.core", types.ModuleType("pylon.core"))
    monkeypatch.setitem(sys.modules, "pylon.core.tools",
                        types.SimpleNamespace(log=types.SimpleNamespace(info=lambda msg, *a: lines.append(msg))))
    monkeypatch.setitem(sys.modules, "tools", types.ModuleType("tools"))
    spec = importlib.util.spec_from_file_location("security_metrics_client_contract", CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._clock = Clock()
    module.schedule_flush = lambda delay, fn: timers.append((delay, fn))
    module.lines, module.timers = lines, timers
    return module


def test_the_accessor_copy_is_byte_identical_to_the_tracing_reference():
    digest = hashlib.sha256(CLIENT_PATH.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    assert digest == EXPECTED_SHA256, (
        "security_metrics_client.py differs from the tracing reference; apply the change in all four repos "
        "and update EXPECTED_SHA256 in each (see the comment above it)")


def test_pinned_constants(client):
    assert (client.ALLOWED_LABEL_KEYS, client.MAX_LABEL_VALUE_LEN, client.LOG_INTERVAL_S) == (
        frozenset({"capability"}), 64, 10.0)


def test_line_format_token_rules_and_rate_cap(client):
    plain = client.SecurityCounter("auth_epoch_mismatch_total", "d")
    labelled = client.SecurityCounter("worker_capability_reject_total", "d", ("capability",))

    plain.inc()
    labelled.inc(capability="task_envelope.v1")
    labelled.inc(capability="x y=1\nz")
    plain.inc()
    plain.inc()
    client._clock.t += 10
    plain.inc()
    labelled.inc(capability="")
    labelled.inc(capability="é" * 100)

    assert client.lines == [
        "security_metric name=auth_epoch_mismatch_total value=1",
        "security_metric name=worker_capability_reject_total value=1 capability=task_envelope.v1",
        "security_metric name=worker_capability_reject_total value=1 capability=x_y_1_z",
        "security_metric name=auth_epoch_mismatch_total value=3",
        "security_metric name=worker_capability_reject_total value=1 capability=-",
        "security_metric name=worker_capability_reject_total value=1 capability=" + "_" * 64,
    ]
    assert plain.total() == 4 and labelled.total() == 4


def test_a_burst_that_stops_inside_the_window_is_flushed_once_at_its_end(client):
    counter = client.SecurityCounter("c_total", "d", ("capability",))

    counter.inc(capability="a")
    client._clock.t += 3
    counter.inc(capability="a")
    counter.inc(2, capability="a")

    assert client.lines == ["security_metric name=c_total value=1 capability=a"]
    assert [round(delay, 6) for delay, _ in client.timers] == [7.0]  # one flush per label set, at the window end
    client._clock.t += 7
    client.timers[0][1]()
    assert client.lines[1:] == ["security_metric name=c_total value=3 capability=a"]
    client.timers[0][1]()  # nothing pending any more
    assert len(client.lines) == 2


def test_a_flush_that_wakes_early_waits_out_the_window(client):
    counter = client.SecurityCounter("c_total", "d")

    counter.inc()
    counter.inc()
    client._clock.t += 10  # window over: the next increment writes the held-back count itself
    counter.inc()
    counter.inc()  # suppressed again, a flush is already armed for the old window
    client._clock.t += 1
    client.timers[0][1]()

    assert client.lines == ["security_metric name=c_total value=1", "security_metric name=c_total value=2"]
    assert len(client.timers) == 2 and round(client.timers[1][0], 6) == 9.0
    client._clock.t += 9
    client.timers[1][1]()
    assert client.lines[2:] == ["security_metric name=c_total value=1"]


@pytest.mark.parametrize("value", [0, -1, 1.5, True, None, "2"])
def test_only_positive_integers_are_counted(client, value):
    counter = client.SecurityCounter("c_total", "d")

    counter.inc(value)

    assert counter.total() == 0 and client.lines == [] and client.timers == []


def test_a_failing_shared_tool_falls_back_to_the_local_count(client):
    class Broken:
        def counter(self, *args):
            raise ValueError("registered with other labels")

    sys.modules["tools"].security_metrics = Broken()
    counter = client.SecurityCounter("c_total", "d", ("capability",))

    counter.inc(capability="a")

    assert counter.total(capability="a") == 1
    assert client.lines == ["security_metric name=c_total value=1 capability=a"]


@pytest.mark.parametrize("key", ["user_id", "project_id", "token", "reason"])
def test_label_keys_outside_the_allow_list_are_refused(client, key):
    with pytest.raises(ValueError):
        client.SecurityCounter("m_total", "d", (key,))


def test_wrong_labels_are_a_caller_error(client):
    counter = client.SecurityCounter("m_total", "d", ("capability",))

    with pytest.raises(ValueError):
        counter.inc()
    with pytest.raises(ValueError):
        counter.inc(capability="a", extra="b")
    assert counter.total() == 0 and client.lines == []
