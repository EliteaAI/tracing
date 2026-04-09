"""
System and Infrastructure Metrics Collector

Collects system-level metrics using psutil and exports them via OpenTelemetry:
- CPU usage (percent, per-core)
- Memory usage (used, available, percent)
- Disk usage (used, free, percent)
- Network I/O (bytes sent/received, connections)
- Process metrics (threads, file descriptors, memory)
"""

import os
import threading
import time
from typing import Optional, Callable

from pylon.core.tools import log

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    log.warning("psutil not available - system metrics disabled")


class SystemMetricsCollector:
    """Collects system metrics and registers them with OpenTelemetry."""

    def __init__(
        self,
        meter,
        service_name: str = "pylon-auth",
        collection_interval: float = 15.0,
        include_per_cpu: bool = False,
        include_disk: bool = True,
        include_network: bool = True,
        include_process: bool = True,
    ):
        """
        Initialize the system metrics collector.

        Args:
            meter: OpenTelemetry Meter instance
            service_name: Service name for metric labels
            collection_interval: How often to collect metrics (seconds)
            include_per_cpu: Include per-CPU core metrics
            include_disk: Include disk I/O metrics
            include_network: Include network I/O metrics
            include_process: Include process-specific metrics
        """
        self.meter = meter
        self.service_name = service_name
        self.collection_interval = collection_interval
        self.include_per_cpu = include_per_cpu
        self.include_disk = include_disk
        self.include_network = include_network
        self.include_process = include_process

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._process = psutil.Process() if PSUTIL_AVAILABLE else None

        # Cache for rate calculations
        self._last_net_io = None
        self._last_disk_io = None
        self._last_collection_time = None

        # Create metrics
        self._create_metrics()

    def _create_metrics(self):
        """Create all system metric instruments."""
        if not PSUTIL_AVAILABLE:
            return

        # CPU Metrics
        self.cpu_percent = self.meter.create_observable_gauge(
            name="system.cpu.utilization",
            description="CPU utilization as a percentage",
            unit="%",
            callbacks=[self._cpu_callback],
        )

        # Memory Metrics
        self.memory_used = self.meter.create_observable_gauge(
            name="system.memory.used",
            description="Memory used in bytes",
            unit="By",
            callbacks=[self._memory_used_callback],
        )

        self.memory_available = self.meter.create_observable_gauge(
            name="system.memory.available",
            description="Memory available in bytes",
            unit="By",
            callbacks=[self._memory_available_callback],
        )

        self.memory_percent = self.meter.create_observable_gauge(
            name="system.memory.utilization",
            description="Memory utilization as a percentage",
            unit="%",
            callbacks=[self._memory_percent_callback],
        )

        # Disk Metrics
        if self.include_disk:
            self.disk_used = self.meter.create_observable_gauge(
                name="system.disk.used",
                description="Disk space used in bytes",
                unit="By",
                callbacks=[self._disk_used_callback],
            )

            self.disk_free = self.meter.create_observable_gauge(
                name="system.disk.free",
                description="Disk space free in bytes",
                unit="By",
                callbacks=[self._disk_free_callback],
            )

            self.disk_percent = self.meter.create_observable_gauge(
                name="system.disk.utilization",
                description="Disk utilization as a percentage",
                unit="%",
                callbacks=[self._disk_percent_callback],
            )

        # Network Metrics
        if self.include_network:
            self.net_bytes_sent = self.meter.create_observable_counter(
                name="system.network.bytes_sent",
                description="Total bytes sent over network",
                unit="By",
                callbacks=[self._net_bytes_sent_callback],
            )

            self.net_bytes_recv = self.meter.create_observable_counter(
                name="system.network.bytes_recv",
                description="Total bytes received over network",
                unit="By",
                callbacks=[self._net_bytes_recv_callback],
            )

            self.net_connections = self.meter.create_observable_gauge(
                name="system.network.connections",
                description="Number of network connections",
                unit="{connections}",
                callbacks=[self._net_connections_callback],
            )

        # Process Metrics
        if self.include_process:
            self.process_cpu_percent = self.meter.create_observable_gauge(
                name="process.cpu.utilization",
                description="Process CPU utilization as a percentage",
                unit="%",
                callbacks=[self._process_cpu_callback],
            )

            self.process_memory_bytes = self.meter.create_observable_gauge(
                name="process.memory.rss",
                description="Process resident set size (RSS) in bytes",
                unit="By",
                callbacks=[self._process_memory_callback],
            )

            self.process_threads = self.meter.create_observable_gauge(
                name="process.threads",
                description="Number of threads in the process",
                unit="{threads}",
                callbacks=[self._process_threads_callback],
            )

            self.process_open_fds = self.meter.create_observable_gauge(
                name="process.open_file_descriptors",
                description="Number of open file descriptors",
                unit="{fds}",
                callbacks=[self._process_fds_callback],
            )

        log.info(f"System metrics collector initialized (interval={self.collection_interval}s)")

    # CPU Callbacks
    def _cpu_callback(self, options):
        """Callback for CPU utilization."""
        try:
            cpu_percent = psutil.cpu_percent(interval=None)
            yield self._observation(cpu_percent, {"cpu": "total"})

            if self.include_per_cpu:
                per_cpu = psutil.cpu_percent(interval=None, percpu=True)
                for i, pct in enumerate(per_cpu):
                    yield self._observation(pct, {"cpu": f"cpu{i}"})
        except Exception as e:
            log.debug(f"Error collecting CPU metrics: {e}")

    # Memory Callbacks
    def _memory_used_callback(self, options):
        try:
            mem = psutil.virtual_memory()
            yield self._observation(mem.used)
        except Exception as e:
            log.debug(f"Error collecting memory metrics: {e}")

    def _memory_available_callback(self, options):
        try:
            mem = psutil.virtual_memory()
            yield self._observation(mem.available)
        except Exception as e:
            log.debug(f"Error collecting memory metrics: {e}")

    def _memory_percent_callback(self, options):
        try:
            mem = psutil.virtual_memory()
            yield self._observation(mem.percent)
        except Exception as e:
            log.debug(f"Error collecting memory metrics: {e}")

    # Disk Callbacks
    def _disk_used_callback(self, options):
        try:
            disk = psutil.disk_usage('/')
            yield self._observation(disk.used, {"mountpoint": "/"})
        except Exception as e:
            log.debug(f"Error collecting disk metrics: {e}")

    def _disk_free_callback(self, options):
        try:
            disk = psutil.disk_usage('/')
            yield self._observation(disk.free, {"mountpoint": "/"})
        except Exception as e:
            log.debug(f"Error collecting disk metrics: {e}")

    def _disk_percent_callback(self, options):
        try:
            disk = psutil.disk_usage('/')
            yield self._observation(disk.percent, {"mountpoint": "/"})
        except Exception as e:
            log.debug(f"Error collecting disk metrics: {e}")

    # Network Callbacks
    def _net_bytes_sent_callback(self, options):
        try:
            net_io = psutil.net_io_counters()
            yield self._observation(net_io.bytes_sent)
        except Exception as e:
            log.debug(f"Error collecting network metrics: {e}")

    def _net_bytes_recv_callback(self, options):
        try:
            net_io = psutil.net_io_counters()
            yield self._observation(net_io.bytes_recv)
        except Exception as e:
            log.debug(f"Error collecting network metrics: {e}")

    def _net_connections_callback(self, options):
        try:
            # Count connections by status
            connections = psutil.net_connections(kind='inet')
            status_counts = {}
            for conn in connections:
                status = conn.status
                status_counts[status] = status_counts.get(status, 0) + 1

            # Yield total and by status
            yield self._observation(len(connections), {"status": "total"})
            for status, count in status_counts.items():
                yield self._observation(count, {"status": status.lower()})
        except (psutil.AccessDenied, PermissionError):
            # net_connections requires elevated privileges on some systems
            log.debug("Cannot access network connections (permission denied)")
        except Exception as e:
            log.debug(f"Error collecting connection metrics: {e}")

    # Process Callbacks
    def _process_cpu_callback(self, options):
        try:
            if self._process:
                cpu_pct = self._process.cpu_percent(interval=None)
                yield self._observation(cpu_pct)
        except Exception as e:
            log.debug(f"Error collecting process CPU metrics: {e}")

    def _process_memory_callback(self, options):
        try:
            if self._process:
                mem_info = self._process.memory_info()
                yield self._observation(mem_info.rss)
        except Exception as e:
            log.debug(f"Error collecting process memory metrics: {e}")

    def _process_threads_callback(self, options):
        try:
            if self._process:
                yield self._observation(self._process.num_threads())
        except Exception as e:
            log.debug(f"Error collecting process thread metrics: {e}")

    def _process_fds_callback(self, options):
        try:
            if self._process:
                # num_fds() is Unix-only
                if hasattr(self._process, 'num_fds'):
                    yield self._observation(self._process.num_fds())
                else:
                    # Windows: use num_handles
                    yield self._observation(self._process.num_handles())
        except Exception as e:
            log.debug(f"Error collecting process FD metrics: {e}")

    def _observation(self, value, attributes: dict = None):
        """Create an observation with service name label."""
        from opentelemetry.metrics import Observation

        attrs = {"service.name": self.service_name}
        if attributes:
            attrs.update(attributes)
        return Observation(value, attrs)

    def start(self):
        """Start the metrics collection (no-op for observable instruments)."""
        if not PSUTIL_AVAILABLE:
            log.warning("Cannot start system metrics - psutil not available")
            return

        # Prime the CPU percent calculation (first call returns 0)
        psutil.cpu_percent(interval=None)
        if self._process:
            self._process.cpu_percent(interval=None)

        self._running = True
        log.info("System metrics collector started")

    def stop(self):
        """Stop the metrics collection."""
        self._running = False
        log.info("System metrics collector stopped")


def create_system_metrics_collector(
    meter,
    service_name: str = "pylon-auth",
    config: dict = None,
) -> Optional[SystemMetricsCollector]:
    """
    Factory function to create a system metrics collector.

    Args:
        meter: OpenTelemetry Meter instance
        service_name: Service name for labeling
        config: Optional configuration dict with keys:
            - collection_interval: float (default 15.0)
            - include_per_cpu: bool (default False)
            - include_disk: bool (default True)
            - include_network: bool (default True)
            - include_process: bool (default True)

    Returns:
        SystemMetricsCollector instance or None if psutil unavailable
    """
    if not PSUTIL_AVAILABLE:
        return None

    config = config or {}
    return SystemMetricsCollector(
        meter=meter,
        service_name=service_name,
        collection_interval=config.get('collection_interval', 15.0),
        include_per_cpu=config.get('include_per_cpu', False),
        include_disk=config.get('include_disk', True),
        include_network=config.get('include_network', True),
        include_process=config.get('include_process', True),
    )
