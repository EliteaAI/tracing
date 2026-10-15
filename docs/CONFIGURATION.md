# Tracing Configuration Reference

Complete reference for `tracing.yml` configuration options.

## Configuration File Location

Each pylon has its own configuration file:

```
pylon_auth/configs/tracing.yml
pylon_main/configs/tracing.yml
pylon_indexer/configs/tracing.yml
```

## Full Configuration Template

```yaml
# Tracing Plugin Configuration
# Copy and customize for each pylon

# =============================================================================
# MASTER SWITCH
# =============================================================================
# Enable/disable all tracing and metrics collection
# Can be overridden by TRACING_ENABLED environment variable
enabled: true

# =============================================================================
# OTLP EXPORTER CONFIGURATION
# =============================================================================
otlp:
  enabled: true
  endpoint: "http://otel-collector:4317"   # gRPC endpoint
  insecure: true                           # Set to false for TLS

# =============================================================================
# INSTRUMENTATION OPTIONS
# =============================================================================
# Toggle individual instrumentation features
instrumentation:
  # Database query tracing (SQLAlchemy)
  # Recommended: true for pylon_main/pylon_auth, false for pylon_indexer
  database: true

  # Outgoing HTTP request tracing (requests library)
  # Traces calls to external APIs, LLM endpoints, etc.
  http_client: true

  # Incoming HTTP request tracing (Flask)
  # Creates SERVER spans for REST API endpoints
  http_requests: true

  # Socket.IO event tracing
  # Traces WebSocket events (chat, real-time updates)
  socket_io: true

  # Log enrichment with trace context
  # Adds trace_id and span_id to log messages
  logging: true

  # Outgoing RPC call tracing (CLIENT spans)
  # Traces calls from this pylon to other pylons
  rpc_calls: true

  # Incoming RPC call tracing (SERVER spans)
  # Traces calls received from other pylons
  rpc_server: true

  # System metrics collection (CPU, memory, disk, network)
  system_metrics: true

# =============================================================================
# METRICS CONFIGURATION
# =============================================================================
metrics:
  # How often metrics are exported to OTEL Collector (milliseconds)
  export_interval_ms: 15000    # 15 seconds

  # System metrics collection options
  system:
    # How often psutil collects metrics (seconds)
    collection_interval: 15.0

    # Include per-CPU core metrics (can be verbose)
    include_per_cpu: false

    # Disk usage metrics
    include_disk: true

    # Network I/O metrics
    include_network: true

    # Process-specific metrics (CPU, memory, threads)
    include_process: true

# =============================================================================
# PAYLOAD CAPTURE CONFIGURATION
# =============================================================================
# Capture request parameters and body in spans
payload_capture:
  enabled: true                # Master switch for payload capture
  max_size: 4096               # Max size of serialized payload (characters)
  max_depth: 5                 # Max depth for nested structures
  additional_mask_keys: []     # Additional keys to mask (added to defaults)
  # Default masked keys: password, secret, token, api_key, authorization,
  # bearer, credential, session, cookie, private

# =============================================================================
# USER CONTEXT CONFIGURATION
# =============================================================================
# Capture user identity in spans for per-user observability
user_context:
  enabled: true                # Master switch for user context capture
  capture_email: false         # Fetch user email via RPC (adds overhead)
                               # When enabled, adds user.email and user.name
                               # Set to true only on pylon_main for best results
  # Sources checked (in priority order, later overrides earlier):
  # 1. OpenTelemetry baggage (propagated from upstream services)
  # 2. Request headers (X-Auth-Type, X-Auth-ID, X-Auth-Reference)
  # 3. Flask g.auth object (highest priority)

# =============================================================================
# EVENTNODE CONFIGURATION (Log Routing)
# =============================================================================
# Routes logs through Redis EventNode to logging_hub for OTEL export
# Set type to "MockEventNode" to disable
event_node:
  type: RedisEventNode          # or "MockEventNode" to disable
  host: ${REDIS_HOST}
  port: ${REDIS_PORT}
  password: ${REDIS_PASSWORD}
  event_queue: ${NAME_PREFIX}_indexer
  hmac_key: ${INDEXER_HMAC_KEY}
  hmac_digest: sha512
  callback_workers: null
  mute_first_failed_connections: 10
  use_ssl: ${REDIS_SSL}

# =============================================================================
# SAMPLING CONFIGURATION
# =============================================================================
# Reduce trace volume in production environments
sampling:
  enabled: false               # Set to true to enable sampling
  rate: 1.0                    # 1.0 = 100%, 0.1 = 10%, 0.01 = 1%

# =============================================================================
# EXCLUSIONS
# =============================================================================
# Endpoints and paths to exclude from tracing
exclude:
  # RPC method names to exclude
  endpoints:
    - "health.check"
    - "monitoring.ping"

  # HTTP paths to exclude (prefix matching)
  paths:
    - "/health"
    - "/metrics"
    - "/favicon.ico"
    - "/static"

  # Socket.IO events to exclude
  socket_events:
    - "connect"
    - "disconnect"
    - "ping"
    - "pong"

# =============================================================================
# SERVICE IDENTIFICATION
# =============================================================================
# Labels applied to all traces and metrics
service:
  name: "pylon-main"           # Change per pylon: pylon-auth, pylon-main, pylon-indexer
  environment: "development"   # development, staging, production
```

## Recommended Configurations by Pylon

### pylon_auth

```yaml
enabled: true
service:
  name: "pylon-auth"
instrumentation:
  database: true
  socket_io: true
  rpc_server: true      # Receives many RPC calls from pylon_main
  system_metrics: true
```

### pylon_main

```yaml
enabled: true
service:
  name: "pylon-main"
instrumentation:
  database: true
  socket_io: true
  rpc_calls: true       # Makes many RPC calls to auth/indexer
  system_metrics: true
```

### pylon_indexer

```yaml
enabled: true
service:
  name: "pylon-indexer"
instrumentation:
  database: false       # Minimal DB usage
  socket_io: false      # No Socket.IO
  http_client: true     # LLM API calls
  system_metrics: true
```

## Environment Variable Overrides

Environment variables take precedence over config file values:

| Variable | Overrides | Example |
|----------|-----------|---------|
| `TRACING_ENABLED` | `enabled` | `TRACING_ENABLED=true` |
| `TRACING_OTLP_ENDPOINT` | `otlp.endpoint` | `TRACING_OTLP_ENDPOINT=http://collector:4317` |

### Docker Compose Example

```yaml
services:
  pylon_main:
    environment:
      - TRACING_ENABLED=true
      - TRACING_OTLP_ENDPOINT=http://otel-collector:4317
```

## Disabling Specific Features

### Disable All Tracing

```yaml
enabled: false
```

### Disable Only System Metrics

```yaml
instrumentation:
  system_metrics: false
```

### Disable RPC Tracing

```yaml
instrumentation:
  rpc_calls: false
  rpc_server: false
```

### Disable Log Routing

```yaml
event_node:
  type: MockEventNode
```

## Production Recommendations

For production environments:

```yaml
# Enable sampling to reduce volume
sampling:
  enabled: true
  rate: 0.1              # Sample 10% of traces

# Adjust metrics interval
metrics:
  export_interval_ms: 60000   # Export every 60 seconds

# Disable verbose metrics
metrics:
  system:
    include_per_cpu: false
    include_network: false    # If not needed
```

## Security Audit Outbox

Schema v1 security audit events (#6945, C-1009) live in the `security_audit_events` table, which is
also the transactional outbox. It is independent of `enabled`, `audit_trail` and Langfuse. The table
is created with `create_all` at plugin init in every pylon that loads this plugin; there is no SQL
migration. Policy is operator config only and is deliberately absent from `admin_schema.json`.

```yaml
security_audit:
  failure_policy: buffer     # buffer (default) | fail_closed
  backlog_limit: 10000       # pending rows above this are "backlog exceeded"
  relay:
    enabled: false           # default off: nothing is delivered, rows accumulate
    interval_s: 5            # poll interval; a full batch is followed immediately by the next
    batch_size: 100
    max_attempts: 10         # then delivery_state = failed
    sink:
      type: jsonl            # only sink in v1
      path: null             # required when relay.enabled
```

- Callers add a row in their own transaction with `utils.security_audit.record_security_event(session, event)`
  (no commit inside), or use `record_security_event_autocommit(event)` when they have no transaction.
  Under `fail_closed` an autocommit failure raises `SecurityAuditUnavailable`; under `buffer` it
  increments `security_audit_drop_total` and returns `None`.
- The relay claims due rows with `FOR UPDATE SKIP LOCKED`, writes one OCSF-shaped JSON object per line
  and retries with full-jitter exponential backoff (base 1 s, cap 300 s). Delivery is at-least-once;
  consumers deduplicate on `metadata.uid` (the `event_id`). Enable it in one pylon (normally `pylon_main`).
- RPC `security_audit_admission_allowed` returns `False` only under `fail_closed` when pending rows exceed
  `backlog_limit` (count cached for 5 s; an unreadable count is treated as exceeded). Nothing calls it yet.
- In-process metrics: `utils.security_audit.security_audit_metrics()` returns
  `security_audit_outbox_depth`, `security_audit_delivery_lag_seconds` (age of the last delivered row)
  and `security_audit_drop_total`.

## Security Metrics

Every C-1009 security counter (#6754) is recorded by one helper, `utils/security_metrics.py`, which the
plugin publishes as `tools.security_metrics` before the `enabled` gate (it exists even with tracing off).
Each increment (a) adds to an OpenTelemetry counter on the `elitea.security` meter, (b) updates an
in-process total per label set, and (c) writes one INFO log line through `pylon.core.tools.log`:

```
security_metric name=<name> value=<n> <label>=<value> ...
```

Label keys follow the metric's declared order. Values are reason codes, capability names or ids; each is
reduced to one token (characters outside `A-Za-z0-9_.:/@+-` become `_`, at most 64 characters). The OTel
export uses the tracing MeterProvider, so it needs `enabled: true`, `otlp.enabled: true` and
`instrumentation.system_metrics` (the default); the log line is always written. Consumer plugins that cannot
find the tool keep a local in-process counter and still write the log line.

| Metric | Type | Labels | Recorded by |
|--------|------|--------|-------------|
| `governed_dispatch_unavailable_total` | counter | none | `elitea_core`: governed task refused, no live worker supports it |
| `worker_capability_reject_total` | counter | `capability` | `indexer_worker`: envelope refused by the worker |
| `auth_epoch_mismatch_total` | counter | none | `auth`: cached auth result built under an older epoch |
| `auth_invalidate_events_total` | counter | none | `auth`: `auth_invalidate` event handled |
| `security_audit_drop_total` | counter | none | `tracing`: audit event lost because the outbox write failed |
| `security_audit_outbox_depth` | observable gauge | none | `tracing`: pending outbox rows, read on each export |
| `security_audit_delivery_lag_seconds` | observable gauge (`s`) | none | `tracing`: age of the last delivered row; no sample before the first delivery |

Example: `security_metric name=worker_capability_reject_total value=1 capability=task_envelope.v1`.

Counter names are kept as they were; the OTel counters carry them unchanged, so a Prometheus-style exporter
that appends `_total` may need a rename rule in the collector.
