# Load Shedding Considerations for Envoy-Fronted Integration Pods

## Context

We operate several hundred Java/Camel/Quarkus integration pods. Each pod is a
proxy (sometimes with orchestration/transformation) to backend APIs we do not
control. Workload profiles vary per pod and per endpoint, and shift over time.

The goal is **proactive load shedding**: detect approaching saturation and begin
rejecting new requests *before* the pod degrades, then recover automatically
once pressure drops.

---

## Architecture & Traffic Flow

```
client → gateway (kong) → [istio/envoy] → app pod → [istio/envoy] → egress pod → backend (external)
```

Three distinct domains with separate saturation characteristics:

| Domain | What saturates it | Who owns it |
|--------|-------------------|-------------|
| **App pod** | CPU, heap, threads, internal queues | Us |
| **Egress pod** | Connection pools, thread pools | Us |
| **Backend** | Unknown — response time varies | External (not us) |

---

## The Latency Attribution Problem

**Critical insight**: end-to-end request latency is ambiguous. At any layer,
observed latency combines multiple independent causes:

1. **Backend response time** — external, uncontrollable, and *totally fine*.
   The pod is just waiting on I/O. This is not a problem to solve.
2. **Normal processing time** — transformation, serialisation, validation,
   TLS. Expected and fine. The pod is doing its job.
3. **Resource exhaustion** — threads starved, CPU saturated, GC thrashing,
   queuing internally. **This IS the problem we're trying to catch.**

All three manifest identically as "higher latency." You cannot distinguish
them from latency alone.

**This is why latency-based shedding (e.g., `adaptive_concurrency`) is
dangerous at any layer.** If a backend slows from 50ms to 5s, the pod's total
request duration rises — but the pod itself may be perfectly healthy with idle
CPU and free threads. Latency-based shedding would reject new requests from a
healthy pod.

**The correct approach**: measure the *cause* directly, not the *symptom*.

| Cause of high latency | How to detect | Action |
|------------------------|---------------|--------|
| Backend slow | Low local CPU + high outbound latency | Timeouts, circuit break the backend |
| Normal processing | Stable CPU, proportional to payload | None — working as designed |
| Resource exhaustion | High CPU, high thread count, GC spikes | **Shed load** |

Resource metrics (CPU, threads, heap, GC) unambiguously tell you the pod is
struggling with its own workload. They are never polluted by downstream
behaviour. This is why shedding decisions must be based on local resource
signals, not latency.

This applies identically at every layer — app pod and egress pod alike. Both
are Camel microservices; both face the same ambiguity.

---

## What Actually Saturates the App Pod

These are **local resource signals** that are not polluted by backend behaviour:

| Signal | Why It Matters | Source |
|--------|----------------|--------|
| Thread pool utilization | Worker threads nearing capacity | MicroProfile / Vert.x metrics |
| CPU utilization % | Approaching compute ceiling | cAdvisor / kubelet |
| Heap utilization % | Approaching OOM | JVM metrics |
| GC pause time / frequency | JVM under memory pressure | JVM metrics |
| Event loop delay (Vert.x) | Internal work queue backing up | Vert.x metrics |
| Internal queue depth | Camel SEDA/direct queue buildup | Camel metrics |

These signals tell you the pod itself is struggling with its own work —
not that it's waiting for someone else.

### What Does NOT Indicate App Pod Saturation

| Signal | Why It's Misleading |
|--------|---------------------|
| End-to-end request latency | Dominated by backend response time |
| In-flight request count (alone) | High in-flight with low CPU = just waiting |
| Backend 5xx rate | Backend's problem, not pod's |
| Upstream connection count | Reflects backend load, not local resource use |

**Exception**: High in-flight count *combined with* high CPU or thread
exhaustion does indicate saturation — the combination matters. 200 in-flight
requests with 5% CPU is fine (they're waiting on I/O). 200 in-flight with 95%
CPU is saturation.

---

## Revised Shedding Strategy

### The Uniform Principle

> **At every layer, shed only based on that layer's own resource consumption.
> Never shed because a downstream layer is slow.**

This applies identically to the app pod AND the egress pod. Both are Camel
microservices; both have the same latency attribution problem. Downstream
slowness is handled with timeouts and circuit breakers, not shedding.

### At the App Pod (Ingress Envoy Sidecar)

**Goal**: Protect the pod from its own resource exhaustion.

**Mechanism**: Runtime-controlled `local_ratelimit` driven by local resource
metrics — NOT by latency.

**Signals to drive shedding**:
- Thread pool > X% utilised
- CPU > Y% sustained
- Heap > Z% (or GC time exceeding threshold)
- Combination: in-flight > N AND CPU > M%

**Why not `adaptive_concurrency` here**: It uses end-to-end latency which
includes backend wait time. A slow backend would incorrectly trigger shedding
on a healthy pod.

**Implementation options**:

1. **In-app shedding** (simplest): A JAX-RS/Vert.x filter that checks local
   thread pool and CPU, rejects with 503 if over threshold. No external
   component needed. The app knows its own state best.

2. **Sidecar controller** (the demo approach): An adjacent process polls
   the app's `/q/metrics` and writes to Envoy's runtime layer when thresholds
   are breached.

3. **Envoy overload manager** (built-in): Envoy has a resource-based overload
   manager that can reject requests based on heap/file-descriptor pressure —
   but it monitors Envoy's own resources, not the app's.

---

### At the Egress Pod (Egress Envoy Sidecar)

**Goal**: Protect the egress pod from its own resource exhaustion, and prevent
slow/failing backends from tying up resources indefinitely.

**The same latency attribution problem applies here.** The egress pod is a
Camel microservice — observed end-to-end latency combines:

1. Egress pod's own processing (TLS, transformation, connection management)
2. Backend response time (external, uncontrollable)

If a backend slows from 50ms to 5s, the egress pod's latency rises — but the
pod itself may be perfectly healthy, just waiting on I/O. Using
`adaptive_concurrency` here has the same flaw as at the app layer: it would
shed load from a healthy egress pod because the backend is slow.

**Shedding mechanism**: Same as app pod — resource-based (CPU, threads, heap).
Shed when the egress pod's own resources are saturated.

**Backend slowness is NOT a shedding trigger — it's handled separately:**

| Problem | Mechanism | Effect |
|---------|-----------|--------|
| Backend slow | Timeouts | Bound wait time, free up threads |
| Backend failing | Circuit breaker (Istio `DestinationRule`) | Stop calling it temporarily |
| Connections held open too long | Connection pool limits + timeouts | Hard cap prevents exhaustion |
| Backend overloaded | Retry budgets | Don't amplify with retries |

**Options for egress pod protection**:

| Approach | Fit |
|----------|-----|
| Resource-based shedding (CPU/threads/heap) | Primary — same as app pod |
| `admission_control` | Useful — if *egress pod itself* starts erroring (not backend errors passed through) |
| Connection pool limits | Essential baseline — prevents slow backends exhausting connections |
| Timeouts (per-backend) | Essential — prevents threads blocked forever |
| Circuit breaker (Istio `DestinationRule`) | Prevents calling a dead/failing backend |
| Retry budgets | Prevents retry storms amplifying load |

**Note on `adaptive_concurrency`**: Not appropriate here for the same reason as
the app pod — it conflates backend latency with egress pod saturation. A slow
backend would trigger shedding on a healthy egress pod.

---

### The Istio Layer Between App and Egress

Istio's Envoy sidecar between app and egress provides:
- **Retry budgets**: Prevent retry storms amplifying load
- **Timeouts**: Bound maximum wait time for any backend call
- **Circuit breaking**: `DestinationRule` outlier detection can remove failing
  egress pods from the pool

These are **backend protection mechanisms, not shedding**. They prevent slow or
failing backends from exhausting the calling pod's resources (threads held open,
connection pools filled). They keep pods healthy so that shedding isn't needed
in the first place.

The distinction:
- Shedding = "I'm overloaded, reject new work to protect myself"
- Timeouts/circuit breakers = "The thing I'm calling is broken, stop waiting
  for it so I stay healthy"

---

## Manual Override (Always Available)

Regardless of automatic mechanisms, the platform team needs a kill switch:

**Runtime-controlled `local_ratelimit`** at both layers:
- Write `shedding.enforced_pct` to Envoy runtime via admin API
- Graduated: 10%, 50%, 100%
- Immediate effect, no restart
- Can be scripted or triggered from runbooks

This is the mechanism from the demo — useful for planned maintenance, known
backend outages, or when the automatic signals haven't caught an issue the
operator can see.

---

## Recommended Layered Architecture

```
                         APP POD SHEDDING                    EGRESS POD SHEDDING
                    (local resource signals)            (local resource signals)
                              │                                    │
client ──▶ [istio] ──▶ app envoy ──▶ app ──▶ [istio] ──▶ egress envoy ──▶ egress ──▶ backend
                         │                                    │                          │
                         ├─ local_ratelimit                   ├─ local_ratelimit         │
                         │  (CPU/heap/threads)                │  (CPU/heap/threads)      │
                         │                                    │                          │
                         └─ manual override                   ├─ manual override         │
                                                              │                          │
                                                              └─ timeouts ───────────────┘
                                                                 circuit breakers
                                                                 connection pool limits
                                                                 retry budgets
```

**Layer 1 — Resource-based shedding (both pods, same mechanism)**:
- Shed when the pod's own CPU/heap/threads are saturated
- Signals: local resource metrics only
- Mechanism: In-app filter or sidecar controller writing to Envoy runtime
- Identical approach for app pods and egress pods

**Layer 2 — Backend protection (egress layer, NOT shedding)**:
- Timeouts: bound wait time, prevent threads blocked forever
- Circuit breakers: stop calling a failing backend
- Connection pool limits: hard cap prevents slow backends exhausting connections
- Retry budgets: prevent retry storms amplifying load
- These are protective mechanisms, not shedding — the egress pod stays healthy
  and responsive, it just stops waiting for broken backends

**Layer 3 — Manual override (both layers)**:
- Platform team kill switch via runtime-controlled `local_ratelimit`
- For planned maintenance, known incidents, emergency response

**Layer 4 — Scaling (complements shedding)**:
- KEDA/HPA watching pod resource metrics
- Scale up when pods are frequently near capacity
- Shedding protects during the scale-up window

---

## Saturation Metrics to Expose from Quarkus Apps

For resource-based shedding decisions, apps should expose via `/q/metrics`:

```
# Local resource signals (what drives app pod shedding)
jvm_threads_live_threads                        # active threads
jvm_threads_peak_threads                        # peak threads (reset periodically)
system_cpu_usage                               # pod CPU fraction (0.0 - 1.0)
process_cpu_usage                              # same, JVM perspective
jvm_memory_used_bytes{area="heap"}             # heap usage
jvm_memory_max_bytes{area="heap"}              # heap limit
jvm_gc_pause_seconds_sum                       # GC time (rate this)
jvm_gc_pause_seconds_count                     # GC frequency

# Request signals (context, not shedding triggers alone)
http_server_active_requests                      # in-flight count
http_server_requests_seconds_count              # request rate (derive)
camel_exchanges_inflight                       # in-flight in Camel routes

# Backend signals (for egress layer, not app pod shedding)
http_client_requests_seconds{quantile="0.99"}  # backend latency
camel_exchanges_failed_total                   # backend errors
```

Quarkus with `quarkus-micrometer-registry-prometheus` exposes most of these
out of the box.

---

## Open Source Projects

| Project | Layer | Role | Fit |
|---------|-------|------|-----|
| **Envoy local_ratelimit + runtime** | Both (app & egress) | Resource-driven shedding + manual override | Best fit — our core mechanism |
| **Envoy overload manager** | Either | Resource-based (Envoy's own resources) | Limited — monitors Envoy not the app |
| **Envoy adaptive_concurrency** | Neither | Latency-based concurrency limiting | **Not appropriate** — conflates downstream latency with local saturation at both layers |
| **Envoy admission_control** | Either | Error-rate based shedding | Useful only if the pod *itself* errors (not pass-through backend errors) |
| **Netflix concurrency-limits** | App | In-JVM adaptive limiting | Same problem — uses latency which includes backend wait |
| **Istio DestinationRule** | Between layers | Circuit breaking, outlier detection | Good for backend protection (not shedding) |
| **KEDA** | Platform | Pod scaling from custom metrics | Complements shedding |
| **resilience4j** | App/Egress | In-app bulkhead/circuit breaker | Per-route backend protection |

**The gap**: There is no off-the-shelf open-source "resource-based shedding
controller" that monitors JVM metrics and drives Envoy runtime. The closest
options are:

1. **Build a simple sidecar** (like our demo controller) — poll `/q/metrics`,
   write to Envoy runtime. Small, focused, fits in a shared container image.
2. **In-app filter** — the app itself checks its own thread pool / CPU and
   returns 503 early. No external component. Simplest operationally.
3. **Prometheus alerting + webhook** — alert fires, webhook writes to Envoy.
   Higher latency but leverages existing infrastructure.

---

## Key Design Principle

> **Shed load at a layer because THAT layer's own resources are saturated.
> Never shed because a downstream layer is slow.**

This applies uniformly to every microservice in the chain:

- App pod sheds because its CPU/heap/threads are exhausted → **correct**
- App pod sheds because egress or backend is slow → **incorrect** (use timeouts)
- Egress pod sheds because its CPU/heap/threads are exhausted → **correct**
- Egress pod sheds because backend is slow → **incorrect** (use timeouts + circuit breakers)

Backend slowness is handled by:
- **Timeouts** — prevent threads being held forever
- **Circuit breakers** — stop calling a dead backend
- **Connection pool limits** — hard cap on outstanding connections
- **Retry budgets** — prevent amplification

These protect the pod's resources without incorrectly declaring the pod unhealthy.

---

## Next Steps

1. **Demo**: Modify the load-shedding demo to show resource-based shedding
   (CPU/threads) with thresholds, confirming it doesn't false-trigger when
   backend is slow but pod is healthy.
2. **Prototype**: Build a minimal sidecar controller that polls Quarkus
   `/q/metrics` for `system_cpu_usage` and `jvm_threads_live_threads`, writes
   to Envoy runtime when thresholds are breached.
3. **Timeout/circuit-breaker config**: Define standard Istio `DestinationRule`
   and timeout configuration for the egress layer to protect against slow
   backends (separate concern from shedding).
4. **Evaluate in-app approach**: Test a simple JAX-RS filter that checks
   thread pool utilisation and rejects with 503 — compare operational
   complexity vs sidecar controller approach.
5. **Production pattern**: Define standard Envoy configs for both app and
   egress pods with `local_ratelimit` (resource-driven shedding) + manual
   override.
