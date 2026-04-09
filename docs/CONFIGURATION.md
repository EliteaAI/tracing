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
