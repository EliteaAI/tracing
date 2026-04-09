# Traces Reference

Complete reference for distributed traces collected by the tracing plugin.

## Overview

Traces provide visibility into request flow across the Elitea platform. Each trace consists of multiple spans representing operations.

## Span Types

### HTTP Request Spans (Flask)

**Config:** `instrumentation.http_requests: true`

Creates SERVER spans for incoming HTTP requests.

| Attribute | Description | Example |
|-----------|-------------|---------|
| `http.method` | HTTP method | `GET`, `POST`, `PUT` |
| `http.url` | Request URL | `/api/v2/prompts/1` |
| `http.status_code` | Response status | `200`, `404`, `500` |
| `http.route` | Flask route | `/api/v2/prompts/<id>` |
| `http.duration_ms` | Request duration | `45.2` |
| `telemetry.data_type` | Routing tag | `api_traces` |

#### User Context Attributes

**Config:** `user_context.enabled: true`

When enabled, HTTP spans include user identity attributes:

| Attribute | Description | Example |
|-----------|-------------|---------|
| `user.id` | User ID from authentication | `3` |
| `user.type` | Auth type (user, token, etc.) | `user` |
| `user.reference` | Session reference (truncated) | `ZOP0ZsFE...` |
| `user.email` | User email (if `capture_email: true`) | `admin@company.com` |
| `user.name` | User display name (if `capture_email: true`) | `John Doe` |
| `project.id` | Project ID from request context | `2` |

**Note:** `user.email` and `user.name` require `user_context.capture_email: true` in config. This makes an RPC call per request to fetch user details, so enable only on `pylon_main` where it provides the most value.

#### Payload Capture Attributes

**Config:** `payload_capture.enabled: true`

When enabled, request parameters are captured (with sensitive data masked):

| Attribute | Description | Example |
|-----------|-------------|---------|
| `http.request.query_params` | Query string params | `{"page": "1", "limit": "10"}` |
| `http.request.body` | Request body (JSON) | `{"prompt": "Hello", "model": "gpt-4"}` |
| `http.request.headers` | Selected headers | `{"content-type": "application/json"}` |

Sensitive fields (password, token, api_key, etc.) are automatically masked as `***MASKED***`.

**Span Name Format:** `HTTP {method} {path}`

**Example:**
```
HTTP GET /api/v2/applications/prompt_lib/1
├── Duration: 45ms
├── Status: OK
└── Attributes:
    ├── http.method: GET
    ├── http.status_code: 200
    └── service.name: pylon-main
```

### RPC Client Spans

**Config:** `instrumentation.rpc_calls: true`

Creates CLIENT spans for outgoing RPC calls to other pylons.

| Attribute | Description | Example |
|-----------|-------------|---------|
| `rpc.system` | RPC system | `pylon` |
| `rpc.method` | Method name | `auth_get_user` |
| `rpc.target_service` | Target pylon | `pylon-auth` |
| `rpc.duration_ms` | Call duration | `12.5` |
| `telemetry.data_type` | Routing tag | `rpc_calls` |

**Span Name Format:** `RPC {method_name}`

**Example:**
```
RPC auth_get_user
├── Kind: CLIENT
├── Duration: 15ms
└── Attributes:
    ├── rpc.method: auth_get_user
    ├── rpc.target_service: pylon-auth
    └── rpc.duration_ms: 15.2
```

### RPC Server Spans

**Config:** `instrumentation.rpc_server: true`

Creates SERVER spans for incoming RPC calls from other pylons.

| Attribute | Description | Example |
|-----------|-------------|---------|
| `rpc.system` | RPC system | `pylon` |
| `rpc.method` | Method name | `auth_get_user` |
| `rpc.service` | This service | `pylon-auth` |
| `rpc.role` | Span role | `server` |
| `rpc.duration_ms` | Processing time | `8.3` |

**Span Name Format:** `RPC Server {method_name}`

**Example:**
```
RPC Server auth_get_user
├── Kind: SERVER
├── Duration: 8ms
└── Attributes:
    ├── rpc.method: auth_get_user
    ├── rpc.service: pylon-auth
    └── rpc.role: server
```

### Socket.IO Event Spans

**Config:** `instrumentation.socket_io: true`

Creates spans for WebSocket event handlers.

| Attribute | Description | Example |
|-----------|-------------|---------|
| `messaging.system` | System type | `socket.io` |
| `messaging.operation` | Event name | `chat_message` |
| `messaging.destination` | Namespace | `/` |
| `telemetry.data_type` | Routing tag | `socketio_events` |

**Span Name Format:** `SocketIO {event_name}`

**Example:**
```
SocketIO chat_message
├── Duration: 120ms
└── Attributes:
    ├── messaging.operation: chat_message
    └── service.name: pylon-main
```

### Database Query Spans

**Config:** `instrumentation.database: true`

Creates spans for SQLAlchemy database operations.

| Attribute | Description | Example |
|-----------|-------------|---------|
| `db.system` | Database type | `postgresql` |
| `db.statement` | SQL query | `SELECT * FROM...` |
| `db.operation` | Operation type | `SELECT`, `INSERT` |

**Example:**
```
postgresql SELECT
├── Duration: 5ms
└── Attributes:
    ├── db.system: postgresql
    └── db.statement: SELECT id, name FROM applications WHERE...
```

### HTTP Client Spans

**Config:** `instrumentation.http_client: true`

Creates CLIENT spans for outgoing HTTP requests (requests library).

| Attribute | Description | Example |
|-----------|-------------|---------|
| `http.method` | HTTP method | `POST` |
| `http.url` | Target URL | `https://api.openai.com/v1/chat` |
| `http.status_code` | Response status | `200` |

**Example:**
```
HTTP POST
├── Kind: CLIENT
├── Duration: 850ms
└── Attributes:
    ├── http.url: https://api.openai.com/v1/chat/completions
    └── http.status_code: 200
```

## Distributed Trace Example

A typical user request flows through multiple services:

```
[Browser]
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ HTTP GET /api/v2/chat/prompt_lib/1/send                     │
│ (pylon-main, SERVER)                                         │
│                                                              │
│  ├── RPC auth_get_user (CLIENT) ─────────────────┐          │
│  │                                                │          │
│  │   ┌────────────────────────────────────────────┼──────┐  │
│  │   │ RPC Server auth_get_user (pylon-auth)      │      │  │
│  │   │ (SERVER)                                   │      │  │
│  │   │  └── postgresql SELECT (users)            │      │  │
│  │   └────────────────────────────────────────────┼──────┘  │
│  │                                                │          │
│  ├── RPC worker_run_agent (CLIENT) ──────────────┐│         │
│  │                                                ││         │
│  │   ┌────────────────────────────────────────────┼┼─────┐  │
│  │   │ RPC Server worker_run_agent (pylon-indexer)││     │  │
│  │   │ (SERVER)                                   ││     │  │
│  │   │  └── HTTP POST api.openai.com (CLIENT)    ││     │  │
│  │   └────────────────────────────────────────────┼┼─────┘  │
│  │                                                ││         │
│  └── Response                                     ││         │
└─────────────────────────────────────────────────────────────┘
```

## Trace Context Propagation

Trace context is automatically propagated:

1. **Between Pylons (RPC):** Via arbiter RPC mechanism
2. **HTTP Requests:** Via W3C Trace Context headers (`traceparent`, `tracestate`)
3. **Logs:** Via log enrichment (`trace_id`, `span_id` in log messages)

## User Context Propagation

User identity is automatically propagated across services using OpenTelemetry Baggage:

1. **pylon_main** extracts user from `g.auth` after authentication
2. User context is stored in baggage (`user_id`, `user_type`, `project_id`, `user_email`)
3. Baggage propagates via W3C `baggage` header to downstream services
4. **pylon_auth**, **pylon_indexer** extract user from baggage (no RPC needed)

This enables per-user observability across the entire request flow without redundant auth lookups.

## Telemetry Data Types

The `telemetry.data_type` attribute enables routing in OTEL Collector:

| Data Type | Description |
|-----------|-------------|
| `api_traces` | REST API requests |
| `rpc_calls` | Inter-service RPC |
| `rpc_server` | Incoming RPC handlers |
| `socketio_events` | WebSocket events |
| `db_queries` | Database operations |
| `http_client` | Outgoing HTTP |

## Viewing Traces

### Dynatrace

1. Navigate to **Distributed Traces**
2. Filter by service: `pylon-main`, `pylon-auth`, `pylon-indexer`
3. Click on a trace to see the full span hierarchy

### Jaeger

1. Select service from dropdown
2. Click **Find Traces**
3. View trace timeline and span details

## Sampling

To reduce trace volume in production:

```yaml
sampling:
  enabled: true
  rate: 0.1    # Sample 10% of traces
```

## Excluding Endpoints

Exclude noisy or uninteresting endpoints:

```yaml
exclude:
  paths:
    - "/health"
    - "/metrics"
    - "/static"
  socket_events:
    - "connect"
    - "disconnect"
    - "ping"
    - "pong"
```

## Example Dynatrace DQL Queries

### Find Unique Users

```dql
fetch spans
| filter isNotNull(user.id)
| summarize users = countDistinct(user.id)
```

### List Users with Email

```dql
fetch spans
| filter isNotNull(user.email)
| summarize by {user.id, user.email, user.name}
| sort user.id asc
```

### User Activity Count (Predict/Chat Operations)

```dql
fetch spans
| filter isNotNull(user.id)
| filter matchesPhrase(span.name, "predict") or matchesPhrase(span.name, "chat")
| summarize requests = count() by user.id
| sort requests desc
```

### Trace Requests for Specific User

```dql
fetch spans
| filter user.id == 3
| fields timestamp, span.name, http.status_code, http.duration_ms
| sort timestamp desc
| limit 100
```

### User Requests by Endpoint

```dql
fetch spans
| filter isNotNull(user.id) and isNotNull(http.route)
| summarize requests = count() by {user.id, http.route}
| sort requests desc
| limit 50
```

### Failed Requests per User

```dql
fetch spans
| filter isNotNull(user.id) and http.status_code >= 400
| summarize errors = count() by {user.id, user.email, http.status_code}
| sort errors desc
```
