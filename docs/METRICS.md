# Metrics Reference

Complete list of metrics collected by the tracing plugin.

## Overview

Metrics are collected using `psutil` and exported via OTLP to the OTEL Collector. All metrics include the following labels:

| Label | Description | Example |
|-------|-------------|---------|
| `service.name` | Pylon service name | `pylon-auth`, `pylon-main`, `pylon-indexer` |

## System Metrics

### CPU Metrics

| Metric Name | Type | Unit | Description |
|-------------|------|------|-------------|
| `system.cpu.utilization` | Gauge | `%` | CPU utilization percentage |

**Labels:**
- `cpu`: `total` or `cpu0`, `cpu1`, etc. (if `include_per_cpu: true`)

**Example in Dynatrace/Grafana:**
```
system.cpu.utilization{service.name="pylon-main", cpu="total"}
```

### Memory Metrics

| Metric Name | Type | Unit | Description |
|-------------|------|------|-------------|
| `system.memory.used` | Gauge | `By` (bytes) | Memory currently in use |
| `system.memory.available` | Gauge | `By` (bytes) | Memory available for allocation |
| `system.memory.utilization` | Gauge | `%` | Memory utilization percentage |

**Example:**
```
system.memory.utilization{service.name="pylon-auth"}
```

### Disk Metrics

| Metric Name | Type | Unit | Description |
|-------------|------|------|-------------|
| `system.disk.used` | Gauge | `By` (bytes) | Disk space used |
| `system.disk.free` | Gauge | `By` (bytes) | Disk space free |
| `system.disk.utilization` | Gauge | `%` | Disk utilization percentage |

**Labels:**
- `mountpoint`: Filesystem mount point (default: `/`)

**Example:**
```
system.disk.utilization{service.name="pylon-indexer", mountpoint="/"}
```

### Network Metrics

| Metric Name | Type | Unit | Description |
|-------------|------|------|-------------|
| `system.network.bytes_sent` | Counter | `By` (bytes) | Total bytes sent |
| `system.network.bytes_recv` | Counter | `By` (bytes) | Total bytes received |
| `system.network.connections` | Gauge | `{connections}` | Number of network connections |

**Labels for connections:**
- `status`: `total`, `established`, `time_wait`, `close_wait`, etc.

**Example:**
```
system.network.connections{service.name="pylon-main", status="established"}
```

## Process Metrics

These metrics are specific to the pylon process itself.

| Metric Name | Type | Unit | Description |
|-------------|------|------|-------------|
| `process.cpu.utilization` | Gauge | `%` | Process CPU usage |
| `process.memory.rss` | Gauge | `By` (bytes) | Resident Set Size (physical memory) |
| `process.threads` | Gauge | `{threads}` | Number of threads |
| `process.open_file_descriptors` | Gauge | `{fds}` | Open file descriptors |

**Example:**
```
process.memory.rss{service.name="pylon-indexer"}
```

## Metric Collection Intervals

| Configuration | Default | Description |
|---------------|---------|-------------|
| `metrics.export_interval_ms` | 15000 | How often metrics are sent to OTEL Collector |
| `metrics.system.collection_interval` | 15.0 | How often psutil collects metrics |

## Enabling/Disabling Metric Categories

In `tracing.yml`:

```yaml
metrics:
  system:
    include_per_cpu: false     # Per-CPU core metrics
    include_disk: true         # Disk metrics
    include_network: true      # Network metrics
    include_process: true      # Process metrics
```

## Viewing Metrics

### Dynatrace

1. Navigate to **Metrics** in Dynatrace
2. Search for `system.` or `process.`
3. Filter by `service.name` dimension

### Grafana (with Prometheus/OTEL backend)

```promql
# CPU usage by service
system_cpu_utilization{cpu="total"}

# Memory usage comparison
system_memory_utilization

# Network connections
sum by (service_name) (system_network_connections{status="established"})
```

### Example Dashboard Queries

**CPU Usage Across Services:**
```promql
avg(system_cpu_utilization{cpu="total"}) by (service_name)
```

**Memory Pressure:**
```promql
system_memory_utilization > 80
```

**Network Connection Count:**
```promql
sum(system_network_connections{status="total"}) by (service_name)
```

## Metric Data Flow

```
psutil (host)
    │
    ▼
SystemMetricsCollector (Python)
    │
    ▼
MeterProvider (OpenTelemetry SDK)
    │
    ▼
PeriodicExportingMetricReader
    │ (every export_interval_ms)
    ▼
OTLPMetricExporter
    │ (gRPC to :4317)
    ▼
OTEL Collector
    │
    ▼
Backend (Dynatrace, Prometheus, etc.)
```

## Troubleshooting Metrics

### Metrics Not Appearing

1. Check `system_metrics: true` in config
2. Verify psutil is installed: `docker compose exec pylon_main pip show psutil`
3. Check logs for initialization: `docker compose logs pylon_main | grep "System metrics"`

### High Cardinality Warning

If `include_per_cpu: true` on a many-core system, you may generate high metric cardinality. Consider keeping it `false` in production.

### Permission Errors

Network connection metrics (`net_connections`) may fail with permission errors on some systems. Check logs:
```
Cannot access network connections (permission denied)
```

This is normal in restricted containers - other metrics will still work.
