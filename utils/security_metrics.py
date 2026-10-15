"""One recording path for every C-1009 security counter (#6754, #6940, #6944, #6945).

Each ``SecurityCounter.inc`` does three things: bumps an OpenTelemetry counter on the global
meter (``elitea.security``; a no-op until the tracing plugin installs a MeterProvider),
keeps an in-process total per label set, and writes one structured INFO log line::

    security_metric name=<name> value=<n> <label>=<value> ...

Label order is the declaration order. Values are reason codes, capability names or ids,
never secrets; they are reduced to a single token so a value cannot forge extra fields.
The instance is published as ``tools.security_metrics`` by the tracing plugin; consumer
plugins keep a local fallback with the same interface (see their ``security_metrics_client``).
"""

import re
import threading

from pylon.core.tools import log

METER_NAME = "elitea.security"
MAX_LABEL_VALUE_LEN = 64
_UNSAFE = re.compile(r"[^A-Za-z0-9_.:/@+-]")


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

    def __init__(self, name, description, labels, instrument_getter):
        self.name = name
        self.description = description
        self.labels = tuple(labels)
        self._instrument_getter = instrument_getter
        self._instrument = None
        self._lock = threading.Lock()
        self._totals = {}

    def inc(self, value=1, **labels) -> None:
        if set(labels) != set(self.labels):
            raise ValueError(f"{self.name} takes labels {list(self.labels)}, got {sorted(labels)}")
        clean = {key: label_value(labels[key]) for key in self.labels}
        key = tuple(clean.values())
        with self._lock:
            self._totals[key] = self._totals.get(key, 0) + value
        # Recording must never break the security path it describes.
        try:
            if self._instrument is None:
                self._instrument = self._instrument_getter(self.name, self.description)
            self._instrument.add(value, clean)
        except Exception as exc:  # pylint: disable=W0718
            log.debug("security_metric %s: OpenTelemetry export failed: %s", self.name, exc)
        try:
            log.info(format_line(self.name, value, clean))
        except Exception:  # pylint: disable=W0718
            pass

    def total(self, **labels) -> int:
        """Sum over every label set that matches the given (possibly partial) labels."""
        wanted = {self.labels.index(k): label_value(v) for k, v in labels.items()}
        with self._lock:
            return sum(n for key, n in self._totals.items()
                       if all(key[i] == v for i, v in wanted.items()))


class SecurityMetrics:
    """Registry of security counters and observable gauges."""

    def __init__(self, meter_getter=None):
        self._meter_getter = meter_getter or self._global_meter
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
                    name, description, labels, self._create_counter)
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
                log.debug("security_metric %s: gauge callback failed: %s", name, exc)
                return []
            return [] if value is None else [Observation(value)]

        with self._lock:
            if name in self._gauges:
                return
            self._gauges[name] = self._meter_getter().create_observable_gauge(
                name, callbacks=[_observe], description=description, unit=unit)


SECURITY_METRICS = SecurityMetrics()
