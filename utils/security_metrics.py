"""One recording path for every C-1009 security counter (#6754, #6940, #6944, #6945).

Each ``SecurityCounter.inc`` does three things: bumps an OpenTelemetry counter on the global
meter (``elitea.security``; a no-op until the tracing plugin installs a MeterProvider),
keeps an in-process total per label set, and writes one structured INFO log line::

    security_metric name=<name> value=<n> <label>=<value> ...

Label order is the declaration order. Values are reason codes or capability names, never
secrets or identifiers; they are reduced to a single token so a value cannot forge extra fields,
and the label keys are limited to ``ALLOWED_LABEL_KEYS``. The log line is rate-capped per counter
and label set (``LOG_INTERVAL_S``): increments inside the window are folded into ``value`` of the
next line, and one flush is scheduled at the end of the window so a burst that stops is still logged.
The OpenTelemetry counter and the in-process total always see every increment.
The instance is published as ``tools.security_metrics`` by the tracing plugin; consumer
plugins keep a local fallback with the same interface (see their ``security_metrics_client``).
"""

import re
import threading
import time

from pylon.core.tools import log

METER_NAME = "elitea.security"
MAX_LABEL_VALUE_LEN = 64
# The only label keys a security counter may declare; values are enum-like codes (#6754).
ALLOWED_LABEL_KEYS = frozenset({"capability"})
# At most one INFO line per counter and label set in this window; the rest is folded into ``value``.
LOG_INTERVAL_S = 10.0
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:/@+-]")


def schedule_flush(delay, fn) -> None:
    """Run ``fn`` once after ``delay`` seconds: a greenlet when gevent is patched in, else a daemon timer."""
    try:
        from gevent import monkey, spawn_later  # pylint: disable=C0415
        if monkey.is_module_patched("threading"):
            spawn_later(delay, fn)
            return
    except ImportError:
        pass
    timer = threading.Timer(delay, fn)
    timer.daemon = True
    timer.start()


def is_count(value) -> bool:
    """An increment is a positive int (bool excluded), so a folded ``value`` is never <= 0."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def label_value(value) -> str:
    """One log/metric token: unsafe characters become ``_`` and the length is bounded."""
    return _UNSAFE.sub("_", str(value))[:MAX_LABEL_VALUE_LEN] or "-"


def format_line(name, value, labels) -> str:
    """The structured log line. ``labels`` is an ordered mapping of already-clean values."""
    parts = ["security_metric", f"name={name}", f"value={value}"]
    parts.extend(f"{key}={val}" for key, val in labels.items())
    return " ".join(parts)


class SecurityCounter:
    """Monotonic counter with a fixed label set."""

    def __init__(self, name, description, labels, instrument_getter, clock=time.monotonic, scheduler=None):
        self.name = name
        self.description = description
        self.labels = tuple(labels)
        if not set(self.labels) <= ALLOWED_LABEL_KEYS:
            raise ValueError(f"{name}: labels {sorted(set(self.labels) - ALLOWED_LABEL_KEYS)} are not allowed")
        self._clock = clock
        self._scheduler = scheduler
        # label values -> [time of the last line, increments not logged yet, flush scheduled]
        self._log_state = {}
        self._instrument_getter = instrument_getter
        self._instrument = None
        self._lock = threading.Lock()
        self._totals = {}

    def inc(self, value=1, **labels) -> None:
        if not is_count(value):
            return
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name} takes labels {list(self.labels)}, got {sorted(labels)}")
        clean = {key: label_value(labels[key]) for key in self.labels}
        key = tuple(clean.values())
        with self._lock:
            self._totals[key] = self._totals.get(key, 0) + value
            to_log, flush_in = self._admit(key, value)
        # Recording must never break the security path it describes.
        try:
            if self._instrument is None:
                self._instrument = self._instrument_getter(self.name, self.description)
            self._instrument.add(value, clean)
        except Exception as exc:  # pylint: disable=W0718
            log.debug("security_metric %s: OpenTelemetry export failed: %s", self.name, type(exc).__name__)
        if to_log:
            self._write(to_log, clean)
        if flush_in is not None:
            self._arm(flush_in, key)

    def _admit(self, key, value):
        """``(value of the line to write now or 0, delay of the flush to schedule or None)``; caller holds the lock."""
        now = self._clock()
        state = self._log_state.setdefault(key, [None, 0, False])
        state[1] += value
        if state[0] is not None and now - state[0] < LOG_INTERVAL_S:
            if state[2]:
                return 0, None
            state[2] = True
            return 0, state[0] + LOG_INTERVAL_S - now
        state[0], to_log, state[1] = now, state[1], 0
        return to_log, None

    def _write(self, value, clean) -> None:
        try:
            log.info(format_line(self.name, value, clean))
        except Exception:  # pylint: disable=W0718
            pass

    def _arm(self, delay, key) -> None:
        """Schedule the end-of-window flush of one label set (already marked as scheduled)."""
        try:
            (self._scheduler or schedule_flush)(delay, lambda: self._flush(key))
        except Exception:  # pylint: disable=W0718
            with self._lock:
                self._log_state[key][2] = False

    def _flush(self, key) -> None:
        """Log what the window held back; wait out the rest of the window if a line was written meanwhile."""
        with self._lock:
            state = self._log_state[key]
            now = self._clock()
            if state[1] == 0:
                state[2] = False
                return
            if now - state[0] < LOG_INTERVAL_S:
                delay, to_log = state[0] + LOG_INTERVAL_S - now, 0
            else:
                delay, to_log = None, state[1]
                state[0], state[1], state[2] = now, 0, False
        if to_log:
            self._write(to_log, dict(zip(self.labels, key)))
        else:
            self._arm(delay, key)

    def total(self, **labels) -> int:
        """Sum over every label set that matches the given (possibly partial) labels."""
        wanted = {self.labels.index(k): label_value(v) for k, v in labels.items()}
        with self._lock:
            return sum(n for key, n in self._totals.items()
                       if all(key[i] == v for i, v in wanted.items()))


class SecurityMetrics:
    """Registry of security counters and observable gauges."""

    def __init__(self, meter_getter=None, clock=time.monotonic, scheduler=None):
        self._meter_getter = meter_getter or self._global_meter
        self._clock = clock
        self._scheduler = scheduler
        self._lock = threading.Lock()
        self._counters = {}
        self._gauges = {}

    @staticmethod
    def _global_meter():
        from opentelemetry import metrics  # pylint: disable=C0415
        return metrics.get_meter(METER_NAME)

    def _create_counter(self, name, description):
        return self._meter_getter().create_counter(name, description=description)

    def counter(self, name, description, labels=()) -> SecurityCounter:
        """Get or create; asking again with other labels is a caller error."""
        with self._lock:
            existing = self._counters.get(name)
            if existing is None:
                existing = self._counters[name] = SecurityCounter(
                    name, description, labels, self._create_counter, self._clock, self._scheduler)
            elif existing.labels != tuple(labels):
                raise ValueError(f"{name} already registered with labels {list(existing.labels)}")
            return existing

    def gauge_callback(self, name, description, fn, unit="1") -> None:
        """Observable gauge sampled by the exporter; ``fn()`` returns a number or None to skip."""
        from opentelemetry.metrics import Observation  # pylint: disable=C0415

        def _observe(_options):
            try:
                value = fn()
            except Exception as exc:  # pylint: disable=W0718
                log.debug("security_metric %s: gauge callback failed: %s", name, type(exc).__name__)
                return []
            return [] if value is None else [Observation(value)]

        with self._lock:
            if name in self._gauges:
                return
            self._gauges[name] = self._meter_getter().create_observable_gauge(
                name, callbacks=[_observe], description=description, unit=unit)


SECURITY_METRICS = SecurityMetrics()
