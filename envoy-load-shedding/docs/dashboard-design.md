# Load Shedding Dashboard Design

## Purpose

Two use cases for dashboards:

1. **Production monitoring** — global view across all integration pods,
   spotting pods approaching saturation before they impact SLAs.
2. **Load testing observation** — during test runs, visualise exactly where
   shedding thresholds would be hit so we can validate our configuration.

---

## Dashboard Structure

Three dashboards, each with a distinct audience and time horizon:

| Dashboard | Audience | Scope | Refresh |
|-----------|----------|-------|---------|
| **Fleet Overview** | Platform team | All pods, aggregated | 30s |
| **Pod Deep Dive** | Developers / incident response | Single pod, full detail | 10s |
| **Load Test** | Performance engineers | Test target pod(s), threshold overlays | 5s |

---

## Fleet Overview Dashboard

Global view: "which pods are near saturation right now?"

### Row 1: Fleet Health Summary

| Panel | Type | Query (PromQL) | Purpose |
|-------|------|----------------|---------|
| Pods in shedding state | Stat | `count(envoy_http_local_rate_limit_rate_limited_total > 0)` | How many pods are actively shedding |
| Pods above 80% CPU | Stat | `count(system_cpu_usage > 0.8)` | Pods approaching CPU ceiling |
| Pods above 80% heap | Stat | `count(jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"} > 0.8)` | Pods approaching OOM |
| Pods with thread exhaustion | Stat | `count(jvm_threads_live_threads / jvm_threads_daemon_threads > 0.9)` | Thread pool near capacity |

### Row 2: Top-N Pods by Saturation Risk

| Panel | Type | Query | Purpose |
|-------|------|-------|---------|
| Top 10 by CPU | Table/Bar | `topk(10, system_cpu_usage)` | Which pods are hottest |
| Top 10 by thread count | Table/Bar | `topk(10, jvm_threads_live_threads)` | Which pods have most threads |
| Top 10 by in-flight | Table/Bar | `topk(10, http_server_active_requests)` | Which pods have most concurrent work |
| Top 10 by heap % | Table/Bar | `topk(10, jvm_memory_used_bytes{area="heap"} / jvm_memory_max_bytes{area="heap"})` | Which pods are memory-tight |

### Row 3: Fleet-Wide Trends (Time Series)

| Panel | Type | Query | Purpose |
|-------|------|-------|---------|
| CPU distribution | Heatmap | `system_cpu_usage` bucketed | See the fleet's CPU profile |
| Thread count distribution | Heatmap | `jvm_threads_live_threads` bucketed | Spot outliers |
| Shedding events over time | Time series | `sum(rate(envoy_http_local_rate_limit_rate_limited_total[5m]))` | Are we shedding more than usual? |
| GC time rate | Time series | `sum by (pod)(rate(jvm_gc_pause_seconds_sum[5m]))` | Fleet-wide GC pressure |

### Row 4: Egress Layer Health

| Panel | Type | Query | Purpose |
|-------|------|-------|---------|
| Backend p99 latency by destination | Time series | `histogram_quantile(0.99, rate(http_client_requests_seconds_bucket[5m]))` | Which backends are slow |
| Backend error rate | Time series | `sum by (destination)(rate(http_client_requests_seconds_count{status=~"5.."}[5m]))` | Which backends are failing |
| Egress concurrency limit (if adaptive_concurrency) | Time series | `envoy_http_adaptive_concurrency_gradient_controller_concurrency_limit` | How restricted is egress |
| Circuit breaker trips | Time series | `envoy_cluster_circuit_breakers_default_cx_open` | Active circuit breaks |

---

## Pod Deep Dive Dashboard

Single pod selected via variable. Full resource detail.

### Row 1: Key Indicators (Stat Panels)

| Panel | Query |
|-------|-------|
| CPU % | `system_cpu_usage{pod="$pod"}` |
| Heap % | `jvm_memory_used_bytes{pod="$pod", area="heap"} / jvm_memory_max_bytes{pod="$pod", area="heap"}` |
| Live threads | `jvm_threads_live_threads{pod="$pod"}` |
| In-flight requests | `http_server_active_requests{pod="$pod"}` |
| Shedding state | `envoy_http_local_rate_limit_enabled{pod="$pod"}` (or runtime value) |
| Request rate | `rate(http_server_requests_seconds_count{pod="$pod"}[1m])` |

### Row 2: Resource Time Series

| Panel | Query | Notes |
|-------|-------|-------|
| CPU over time | `system_cpu_usage{pod="$pod"}` | With threshold line at shedding trigger (e.g., 85%) |
| Heap over time | `jvm_memory_used_bytes{pod="$pod", area="heap"}` | With max line overlay |
| Thread count | `jvm_threads_live_threads{pod="$pod"}` | With threshold line |
| GC pauses | `rate(jvm_gc_pause_seconds_sum{pod="$pod"}[1m])` | Spikes indicate pressure |

### Row 3: Request Behaviour

| Panel | Query | Notes |
|-------|-------|-------|
| In-flight requests | `http_server_active_requests{pod="$pod"}` | The concurrency curve |
| Request rate (RPS) | `rate(http_server_requests_seconds_count{pod="$pod"}[1m])` | Incoming demand |
| Response status breakdown | `sum by (status)(rate(http_server_requests_seconds_count{pod="$pod"}[1m]))` | 200s vs 429s vs 503s |
| Processing time (excluding backend wait) | See note below | True app pod latency |

### Row 4: Backend Interaction (from this pod)

| Panel | Query | Notes |
|-------|-------|-------|
| Outbound latency by destination | `histogram_quantile(0.99, rate(http_client_requests_seconds_bucket{pod="$pod"}[5m]))` | Backend latency this pod sees |
| Outbound in-flight | `camel_exchanges_inflight{pod="$pod"}` | Requests waiting for backends |
| Outbound errors | `rate(camel_exchanges_failed_total{pod="$pod"}[5m])` | Backend failures |

#### Note: Measuring True App Processing Time

To separate "time the app spends doing work" from "time waiting for backends":

```
app_processing_time = total_request_duration - backend_call_duration
```

If the app makes one backend call per request:
```promql
histogram_quantile(0.99, rate(http_server_requests_seconds_bucket{pod="$pod"}[5m]))
-
histogram_quantile(0.99, rate(http_client_requests_seconds_bucket{pod="$pod"}[5m]))
```

This is approximate (doesn't account for parallel calls or multiple backends)
but gives a useful signal. The real answer is to instrument the app to emit a
gauge/histogram for "local processing time" explicitly.

---

## Load Test Dashboard

Designed for active load test sessions. Threshold lines make it obvious when
shedding would engage.

### Variables

- `$pod` — target pod(s) under test
- `$cpu_threshold` — CPU shedding threshold (default: 0.85)
- `$thread_threshold` — Thread shedding threshold (default: 150)
- `$heap_threshold` — Heap shedding threshold (default: 0.80)

### Row 1: Live Status

| Panel | Type | Content |
|-------|------|---------|
| Would be shedding? | Stat (red/green) | `system_cpu_usage > $cpu_threshold OR jvm_threads_live_threads > $thread_threshold OR heap% > $heap_threshold` |
| Shedding actually active? | Stat | Runtime value of `shedding.enforced_pct` |
| Current load (RPS) | Stat | `rate(http_server_requests_seconds_count[30s])` |
| Response mix | Pie | 200 / 429 / 503 / other |

### Row 2: Saturation Curves with Threshold Lines

| Panel | Type | Key Feature |
|-------|------|-------------|
| CPU with threshold | Time series | Horizontal line at `$cpu_threshold` |
| Thread count with threshold | Time series | Horizontal line at `$thread_threshold` |
| Heap % with threshold | Time series | Horizontal line at `$heap_threshold` |
| Combined saturation score | Time series | `max(cpu/threshold, threads/threshold, heap/threshold)` — hits 1.0 at shedding point |

### Row 3: Effect of Shedding

| Panel | Type | Query | Purpose |
|-------|------|-------|---------|
| 429 rate over time | Time series | `rate(envoy_http_local_rate_limit_rate_limited_total[30s])` | When shedding engages |
| Accepted vs rejected | Stacked area | ok vs rate_limited from envoy stats | Visual split |
| Resource recovery after shed | Time series | CPU/threads overlaid with shed events | Confirm resources drop post-shed |

### Row 4: Load Generator Context

| Panel | Type | Query/Source | Purpose |
|-------|------|--------------|---------|
| Injected RPS | Time series | From loadgen metrics or annotation | What we're sending |
| Response latency (client-side) | Time series | From loadgen | What callers experience |
| Error rate (client-side) | Time series | From loadgen | What callers see |

---

## What We Can Deploy Immediately

These queries work with standard Quarkus Micrometer metrics and Envoy stats
that we already have (or can enable with zero code changes):

### Available Now (Quarkus + Micrometer)

| Metric | Available | Notes |
|--------|-----------|-------|
| `system_cpu_usage` | ✅ | Requires `quarkus-micrometer-registry-prometheus` |
| `jvm_memory_used_bytes` | ✅ | Standard JVM metrics |
| `jvm_memory_max_bytes` | ✅ | Standard JVM metrics |
| `jvm_threads_live_threads` | ✅ | Standard JVM metrics |
| `jvm_gc_pause_seconds_sum` | ✅ | Standard JVM metrics |
| `http_server_requests_seconds_*` | ✅ | Quarkus HTTP server metrics |
| `http_server_active_requests` | ✅ | Requires Quarkus 3.x |
| `http_client_requests_seconds_*` | ✅ | If using Quarkus REST client |
| `camel_exchanges_inflight` | ✅ | If `camel-quarkus-micrometer` is present |

### Available Now (Envoy / Istio)

| Metric | Available | Notes |
|--------|-----------|-------|
| `envoy_http_downstream_rq_active` | ✅ | In-flight at Envoy level |
| `envoy_cluster_upstream_rq_active` | ✅ | In-flight to upstream |
| `envoy_http_local_rate_limit_ok` | ✅ | Once local_ratelimit filter is added |
| `envoy_http_local_rate_limit_rate_limited` | ✅ | Once local_ratelimit filter is added |
| `envoy_cluster_circuit_breakers_*` | ✅ | Existing circuit breaker stats |

### Needs Implementation

| Metric | What's Needed |
|--------|---------------|
| `app_processing_time` (local only) | Custom Micrometer timer excluding backend call duration |
| Shedding runtime state | Expose current `shedding.enforced_pct` as a gauge (scrape from Envoy admin `/runtime`) |
| Combined saturation score | Prometheus recording rule combining CPU + threads + heap into 0-1 score |

---

## Prometheus Recording Rules (Recommended)

Pre-compute expensive or composite queries for dashboard efficiency:

```yaml
groups:
  - name: shedding_signals
    interval: 15s
    rules:
      # Heap percentage
      - record: app:heap_usage_ratio
        expr: >
          jvm_memory_used_bytes{area="heap"}
          / jvm_memory_max_bytes{area="heap"}

      # Saturation score (0 = idle, 1 = at shedding threshold)
      - record: app:saturation_score
        expr: >
          clamp_max(
            max without(instance) (
              vector(0),
              system_cpu_usage / 0.85,
              jvm_threads_live_threads / 150,
              app:heap_usage_ratio / 0.80
            ),
            1.5
          )

      # Pods that would be shedding (score >= 1.0)
      - record: app:would_shed
        expr: app:saturation_score >= 1.0

      # Fleet shedding count
      - record: fleet:pods_shedding_count
        expr: count(app:would_shed == 1) OR vector(0)
```

---

## Alerts (Complement the Dashboards)

| Alert | Condition | Severity | Action |
|-------|-----------|----------|--------|
| PodApproachingSaturation | `app:saturation_score > 0.8` for 2m | warning | Investigate, consider scaling |
| PodShedding | `envoy_http_local_rate_limit_rate_limited_total` increasing | warning | Shedding is active — is it expected? |
| PodSustainedShedding | Shedding active for > 5m | critical | Pod needs scaling or investigation |
| FleetWideStress | `fleet:pods_shedding_count > 10` | critical | Systemic issue — backends? |

---

## Implementation Order

1. **Immediate**: Deploy the Load Test dashboard with threshold overlays.
   Requires only standard Quarkus metrics + Grafana. Use during next load test
   to validate threshold values.

2. **Week 1**: Deploy Fleet Overview dashboard. Proves value with existing
   metrics before any shedding mechanism is in place — shows where shedding
   *would* have kicked in historically.

3. **Week 2**: Add recording rules and alerts. Start getting notified about
   pods approaching saturation.

4. **Week 3**: Deploy Pod Deep Dive dashboard. Use during incident response
   to quickly understand a specific pod's state.

5. **Ongoing**: Refine thresholds based on observed load test data. The
   dashboards will show you exactly where the knee in the curve is for
   each workload — that's your shedding threshold.
