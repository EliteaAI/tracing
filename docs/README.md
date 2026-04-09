# Elitea Platform Observability Guide

This guide explains how to enable and configure OpenTelemetry (OTEL) tracing and metrics for the Elitea platform.

## Overview

The tracing plugin provides distributed tracing and system metrics collection across all Elitea pylon services:

| Service | Purpose |
|---------|---------|
| `pylon-auth` | Authentication service |
| `pylon-main` | Platform APIs and core services |
| `pylon-indexer` | Agent runtime and SDK execution |

All telemetry data is exported via OTLP (OpenTelemetry Protocol) to an OTEL Collector, which can then forward to various backends like Dynatrace, Jaeger, Grafana, etc.

## Architecture

```
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│ pylon-auth  │    │ pylon-main  │    │pylon-indexer│
│  (tracing)  │    │  (tracing)  │    │  (tracing)  │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                  │
       │    OTLP (gRPC :4317)                │
       └──────────────────┼──────────────────┘
                          ▼
                ┌─────────────────┐
                │  OTEL Collector │
                │    :4317        │
                └────────┬────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
    ┌──────────┐  ┌──────────┐  ┌──────────┐
    │ Dynatrace│  │  Jaeger  │  │ Grafana  │
    └──────────┘  └──────────┘  └──────────┘
```

## Quick Start

### 1. Prerequisites

- OTEL Collector running and accessible at `http://otel-collector:4317`
- Redis (for EventNode log routing, optional)

### 2. Enable Tracing

Edit the tracing configuration file for each pylon:

| Pylon | Config File |
|-------|-------------|
| pylon_auth | `pylon_auth/configs/tracing.yml` |
| pylon_main | `pylon_main/configs/tracing.yml` |
| pylon_indexer | `pylon_indexer/configs/tracing.yml` |

Set `enabled: true`:

```yaml
enabled: true
```

### 3. Restart Services

```bash
docker compose restart pylon_auth pylon_main pylon_indexer
```

### 4. Verify

Check logs for successful initialization:

```bash
docker compose logs pylon_auth | grep -E "(Tracing|Metrics|System metrics)"
```

Expected output:
```
Tracing plugin is ENABLED - initializing OpenTelemetry...
OpenTelemetry initialized - sending traces to http://otel-collector:4317
System metrics collector initialized (interval=15.0s)
System metrics collector started
OpenTelemetry Metrics initialized
```

## Documentation Index

| Document | Description |
|----------|-------------|
| [Configuration Reference](./CONFIGURATION.md) | Full configuration options |
| [Metrics Reference](./METRICS.md) | List of all collected metrics |
| [Traces Reference](./TRACES.md) | Types of traces and spans |
| [Troubleshooting](./TROUBLESHOOTING.md) | Common issues and solutions |

## What Gets Collected

### Traces (Spans)

- HTTP requests (Flask endpoints)
- Socket.IO events
- RPC calls between pylons (CLIENT and SERVER spans)
- Database queries (SQLAlchemy)
- Outgoing HTTP requests

### Metrics

- CPU utilization (system and process)
- Memory usage (system and process)
- Disk usage
- Network I/O
- Process threads and file descriptors

## Environment Variable Overrides

| Variable | Description | Example |
|----------|-------------|---------|
| `TRACING_ENABLED` | Override enabled flag | `true` / `false` |
| `TRACING_OTLP_ENDPOINT` | Override OTLP endpoint | `http://collector:4317` |

## Support

For issues or questions about observability configuration, refer to:
- [Troubleshooting Guide](./TROUBLESHOOTING.md)
- Platform documentation
