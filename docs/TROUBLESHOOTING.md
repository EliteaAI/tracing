# Troubleshooting Guide

Common issues and solutions for the Elitea tracing plugin.

## Quick Diagnostics

### Check Tracing Status

```bash
# Check if tracing is enabled
docker compose logs pylon_main | grep -E "(Tracing|tracing)" | head -10

# Check metrics initialization
docker compose logs pylon_main | grep -E "(metrics|Metrics)" | head -10

# Check OTEL Collector connectivity
docker compose logs otel-collector | tail -20
```

### Expected Startup Messages

When tracing is working correctly, you should see:

```
Tracing plugin is ENABLED - initializing OpenTelemetry...
Gevent spawn patched for contextvars propagation across greenlets
OpenTelemetry initialized - sending traces to http://otel-collector:4317
System metrics collector initialized (interval=15.0s)
System metrics collector started
OpenTelemetry Metrics initialized
RpcNode.register patched for execution-time RPC server tracing
Requests instrumentation enabled for HTTP client tracing
Socket.IO tracing enabled (excluding: ['connect', 'disconnect', 'ping', 'pong'])
Flask tracing middleware enabled
SQLAlchemy instrumentation enabled for database tracing
RPC client instrumentation enabled for inter-service tracing
Wrapped 98 existing RPC handlers with SERVER-side tracing
```

## Common Issues

### Issue: "Tracing plugin is DISABLED"

**Symptom:**
```
Tracing plugin is DISABLED (set enabled: true in config or TRACING_ENABLED=true)
```

**Solutions:**

1. Check config file:
   ```yaml
   # In pylon_*/configs/tracing.yml
   enabled: true
   ```

2. Or set environment variable:
   ```bash
   export TRACING_ENABLED=true
   docker compose restart pylon_main
   ```

### Issue: Traces Not Appearing in Backend

**Possible Causes:**

1. **OTEL Collector not running:**
   ```bash
   docker compose ps otel-collector
   # Should show "Up"
   ```

2. **Wrong endpoint:**
   ```yaml
   otlp:
     endpoint: "http://otel-collector:4317"  # Check this is correct
   ```

3. **Network connectivity:**
   ```bash
   docker compose exec pylon_main curl -v http://otel-collector:4317
   ```

4. **Collector not forwarding:**
   Check OTEL Collector configuration for exporters.

### Issue: System Metrics Not Appearing

**Symptom:**
```
System metrics collector not available (psutil missing?)
```

**Solution:**
Verify psutil is installed:
```bash
docker compose exec pylon_main pip show psutil
```

If missing, check `requirements.txt` includes:
```
psutil>=5.9.0
```

### Issue: "Failed to initialize metrics"

**Symptom:**
```
Failed to initialize metrics: [error message]
```

**Common Causes:**

1. **OTEL Collector unreachable:**
   ```bash
   docker compose logs otel-collector | grep -i error
   ```

2. **Port conflict:**
   Ensure nothing else is using port 4317.

3. **Missing dependencies:**
   ```bash
   docker compose exec pylon_main pip list | grep opentelemetry
   ```

### Issue: RPC Handlers Not Traced

**Symptom:**
No SERVER spans for RPC calls in pylon_auth.

**Check:**
```bash
docker compose logs pylon_auth | grep "RPC handlers"
# Should show: "Wrapped 98 existing RPC handlers with SERVER-side tracing"
```

**Solution:**
Ensure config has:
```yaml
instrumentation:
  rpc_server: true
```

### Issue: High Memory Usage

**Symptom:**
Process memory growing after enabling tracing.

**Solutions:**

1. **Enable sampling:**
   ```yaml
   sampling:
     enabled: true
     rate: 0.1  # Sample only 10%
   ```

2. **Increase export interval:**
   ```yaml
   metrics:
     export_interval_ms: 60000  # Export every 60 seconds
   ```

3. **Disable verbose metrics:**
   ```yaml
   metrics:
     system:
       include_per_cpu: false
       include_network: false
   ```

### Issue: Log Messages Missing trace_id

**Symptom:**
Logs show `trace_id=00000000000000000000000000000000`

**Explanation:**
This is normal for logs outside of an active span context. Logs within HTTP requests, RPC calls, etc. will have valid trace IDs.

### Issue: "Cannot access network connections (permission denied)"

**Symptom:**
```
Cannot access network connections (permission denied)
```

**Explanation:**
This is a permission issue with `psutil.net_connections()` in containers. It's non-fatal - other metrics still work.

**Solution:**
If you don't need connection metrics:
```yaml
metrics:
  system:
    include_network: false
```

### Issue: EventNode Log Routing Failing

**Symptom:**
```
Failed to set up EventNode log routing: [error]
```

**Check:**
1. Redis is running and accessible
2. Environment variables are set:
   - `REDIS_HOST`
   - `REDIS_PORT`
   - `REDIS_PASSWORD`

**Disable if not needed:**
```yaml
event_node:
  type: MockEventNode
```

## Verification Commands

### Verify Traces Are Being Sent

```bash
# Check for span export logs
docker compose logs pylon_main 2>&1 | grep -i "export"

# Check OTEL Collector receiving data
docker compose logs otel-collector 2>&1 | grep -i "traces"
```

### Verify Metrics Are Being Collected

```bash
# Trigger a metrics collection cycle (wait 15+ seconds after restart)
sleep 20
docker compose logs pylon_main 2>&1 | grep -i "metric"
```

### Test Trace Generation

Make an API request and check for spans:

```bash
# Make a test request
curl http://localhost/api/v2/health

# Check logs for the span
docker compose logs pylon_main 2>&1 | grep "HTTP GET"
```

## Debug Mode

For detailed debugging, you can enable OpenTelemetry debug logging:

```bash
# Set in docker-compose.yml or environment
OTEL_LOG_LEVEL=debug
```

**Warning:** This generates a lot of output. Only use temporarily.

## Getting Help

If issues persist:

1. Collect logs:
   ```bash
   docker compose logs pylon_main > pylon_main.log 2>&1
   docker compose logs pylon_auth > pylon_auth.log 2>&1
   docker compose logs otel-collector > otel_collector.log 2>&1
   ```

2. Check configuration:
   ```bash
   cat pylon_main/configs/tracing.yml
   cat pylon_auth/configs/tracing.yml
   ```

3. Verify versions:
   ```bash
   docker compose exec pylon_main pip list | grep opentelemetry
   ```
