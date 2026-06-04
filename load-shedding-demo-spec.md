# Load-shedding demo: runtime-controlled Envoy filter

A docker-compose stack illustrating runtime-controlled load shedding via
Envoy's `local_ratelimit` filter, toggled by an external HTTP controller.

## Goal

Demonstrate that a request flow can be switched between "pass through" and
"reject with 429" state by writing to Envoy's runtime layer via its admin API,
with no Envoy reload, no static rate threshold, and no impact on in-flight
requests.

This is a mechanism demo. The controller is a manually-triggered HTTP server,
not a real saturation-driven control loop. The real version would scrape
application metrics and decide autonomously; this version exposes
`/shed/on`, `/shed/off`, and `/shed/<pct>` endpoints so the operator drives
the state changes.

## Architecture

```
loadgen ──HTTP──▶ envoy:8080 ──HTTP──▶ app:5678
                     ▲
                     │ POST /runtime_modify
                     │
                  controller:8081 ◀──HTTP── operator (curl)
```

All services on a single Docker network. Inter-service traffic by service name.

## Components

| Service | Role | Image | Ports (host:container) |
|---|---|---|---|
| `app` | Backend that returns 200 on `/` | `hashicorp/http-echo` | (internal only) |
| `envoy` | Front proxy; hosts the `local_ratelimit` filter and exposes the admin API | `envoyproxy/envoy:v1.31-latest` | `8080:8080` (proxy), `9901:9901` (admin) |
| `controller` | HTTP server that POSTs to Envoy's `/runtime_modify` | Custom (Python + Flask) | `8081:8081` |
| `loadgen` | Constant-rate request generator against Envoy | `williamyeh/hey` | (internal only) |

## Key design points

These are load-bearing for the demo and should not be "simplified away" during
implementation:

1. **Token bucket configured with zero tokens** (`max_tokens: 0`,
   `tokens_per_fill: 0`). There is no static "sustainable rate" anywhere in
   the system. Any request consulting the filter exceeds the (empty) bucket.
   Shedding behaviour is controlled entirely by the runtime-controlled
   enforcement percentage.

2. **`filter_enabled` defaults to 100 (always evaluating)**. This means the
   filter records metrics about what it *would* shed even when enforcement is
   off — "shadow mode is free."

3. **`filter_enforced` defaults to 0 (never rejecting)**. The baseline state
   is that all requests pass through. The controller raises this value to
   engage shedding.

4. **`admin_layer` declared in `layered_runtime`**. Without this, writes to
   `/runtime_modify` do not take effect. This is the single most common
   misconfiguration.

5. **Rejection status code set to 429** (`status.code: TooManyRequests`).
   Explicit even though it matches the default — makes the config
   self-documenting.

6. **In-flight requests are unaffected by runtime changes.** A request that
   has already passed the filter and is mid-flight to `app` will complete
   normally regardless of subsequent shedding state changes. The demo should
   make this property visible (see "Observable behaviours" below).

## File layout

```
.
├── docker-compose.yml
├── envoy.yaml
├── controller/
│   ├── Dockerfile
│   └── controller.py
└── README.md
```

## `docker-compose.yml`

```yaml
services:
  app:
    image: hashicorp/http-echo:latest
    command: ["-text=hello", "-listen=:5678"]
    networks: [demo]

  envoy:
    image: envoyproxy/envoy:v1.31-latest
    command: ["-c", "/etc/envoy/envoy.yaml", "--log-level", "info"]
    volumes:
      - ./envoy.yaml:/etc/envoy/envoy.yaml:ro
    ports:
      - "8080:8080"
      - "9901:9901"
    networks: [demo]
    depends_on: [app]

  controller:
    build: ./controller
    ports:
      - "8081:8081"
    networks: [demo]
    depends_on: [envoy]

  loadgen:
    image: williamyeh/hey:latest
    command: ["-z", "10m", "-q", "50", "-c", "10", "http://envoy:8080/"]
    networks: [demo]
    depends_on: [envoy]

networks:
  demo: {}
```

## `envoy.yaml`

```yaml
admin:
  address:
    socket_address: { address: 0.0.0.0, port_value: 9901 }

layered_runtime:
  layers:
    - name: admin_layer
      admin_layer: {}

static_resources:
  listeners:
    - name: ingress
      address:
        socket_address: { address: 0.0.0.0, port_value: 8080 }
      filter_chains:
        - filters:
            - name: envoy.filters.network.http_connection_manager
              typed_config:
                "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
                stat_prefix: ingress_http
                codec_type: AUTO
                route_config:
                  name: local_route
                  virtual_hosts:
                    - name: backend
                      domains: ["*"]
                      routes:
                        - match: { prefix: "/" }
                          route: { cluster: app_cluster }
                http_filters:
                  - name: envoy.filters.http.local_ratelimit
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
                      stat_prefix: shedding
                      token_bucket:
                        max_tokens: 0
                        tokens_per_fill: 0
                        fill_interval: 1s
                      filter_enabled:
                        default_value: { numerator: 100, denominator: HUNDRED }
                        runtime_key: shedding.enabled_pct
                      filter_enforced:
                        default_value: { numerator: 0, denominator: HUNDRED }
                        runtime_key: shedding.enforced_pct
                      status: { code: TooManyRequests }
                  - name: envoy.filters.http.router
                    typed_config:
                      "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router

  clusters:
    - name: app_cluster
      connect_timeout: 1s
      type: STRICT_DNS
      lb_policy: ROUND_ROBIN
      load_assignment:
        cluster_name: app_cluster
        endpoints:
          - lb_endpoints:
              - endpoint:
                  address:
                    socket_address: { address: app, port_value: 5678 }
```

## `controller/controller.py`

```python
from flask import Flask
import requests

ENVOY_ADMIN = "http://envoy:9901"
app = Flask(__name__)


def set_runtime(enforced_pct: int) -> str:
    r = requests.post(
        f"{ENVOY_ADMIN}/runtime_modify",
        params={"shedding.enforced_pct": str(enforced_pct)},
        timeout=2,
    )
    r.raise_for_status()
    return r.text


@app.post("/shed/on")
def shed_on():
    return set_runtime(100), 200


@app.post("/shed/off")
def shed_off():
    return set_runtime(0), 200


@app.post("/shed/<int:pct>")
def shed_pct(pct: int):
    if not 0 <= pct <= 100:
        return "pct must be 0-100", 400
    return set_runtime(pct), 200


@app.get("/health")
def health():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)
```

## `controller/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir flask requests
COPY controller.py .
EXPOSE 8081
CMD ["python", "controller.py"]
```

## Running

```bash
docker compose up --build
```

`loadgen` starts firing 50 RPS at Envoy immediately. Open a second terminal
to drive the controller and inspect Envoy state.

## Observable behaviours

These are the demonstrations the stack should support. Each maps to a property
of the design.

### 1. Baseline pass-through

State: controller untouched, defaults in effect.

Expected:
- All loadgen requests return 200.
- `curl 'localhost:9901/stats?filter=shedding'` shows
  `http_local_rate_limit.shedding.ok` incrementing,
  `http_local_rate_limit.shedding.rate_limited` at zero.
- `curl localhost:9901/runtime` shows `shedding.enforced_pct` at its default
  (0) with no admin-layer override.

This demonstrates that the filter is evaluating in shadow mode without
affecting traffic.

### 2. Full shed

```bash
curl -X POST localhost:8081/shed/on
```

Expected:
- Loadgen requests immediately start returning 429 (visible in hey's
  status-code histogram on completion, or in real time by switching loadgen
  to a curl-loop variant).
- `rate_limited` counter climbs, `ok` counter flat.
- Change takes effect within a single request — no reload, no propagation
  delay.
- `curl localhost:9901/runtime` shows `shedding.enforced_pct: 100` in the
  admin layer.

### 3. Graduated shed

```bash
curl -X POST localhost:8081/shed/50
```

Expected:
- Roughly half of loadgen requests return 200, half return 429.
- Ratio is statistical, not deterministic — the filter samples per request.

This demonstrates the continuous-knob property: shedding intensity is
controlled by a single 0–100 percentage, with no static rate involved.

### 4. Recovery

```bash
curl -X POST localhost:8081/shed/off
```

Expected:
- All requests return 200 again immediately.
- No queue to drain, no state to reset.

### 5. In-flight requests are unaffected

Optional but worth doing. Replace `app` with a deliberately slow responder,
e.g.:

```yaml
  app:
    image: kennethreitz/httpbin
    networks: [demo]
```

Point loadgen at `http://envoy:8080/delay/3` so each request takes ~3s.

Sequence:
1. Start the stack. Requests are in-flight, taking ~3s each.
2. `curl -X POST localhost:8081/shed/on` mid-stream.
3. Observe that:
   - Requests that were *already past Envoy* (in flight to httpbin) still
     complete with 200 after their ~3s delay.
   - Requests arriving *after* the shed-on call immediately return 429.

This demonstrates the property that runtime changes affect only new
admissions, not in-flight work. This is the property that makes graceful
shedding possible — the pod can finish what it's doing while refusing new
work.

## Inspecting Envoy state

Useful admin endpoints during the demo:

| Endpoint | Purpose |
|---|---|
| `GET /runtime` | Current runtime values and their layer of origin |
| `GET /stats?filter=shedding` | Filter counters (ok, rate_limited, etc.) |
| `GET /stats/prometheus` | All stats in Prometheus format |
| `GET /config_dump` | Full effective config — useful for verifying the filter is wired up correctly |
| `POST /runtime_modify?key=value` | Direct runtime write (what the controller calls) |

## What this does NOT demonstrate

Deliberate exclusions, to be clear about scope:

- **Saturation-driven control.** The controller has no input from app
  metrics. A real version would scrape `/metrics` and run a hysteresis loop.
- **Continuous reassertion.** The controller writes a value once per call.
  A production controller would write every N seconds so that a sidecar
  crash mid-shed self-clears via Envoy's static defaults.
- **Multiple replicas / load balancing.** Single `app` instance; no
  per-replica shedding behaviour.
- **Connection-pool ceiling.** No `DestinationRule`-equivalent
  `connectionPool` limit acting as a hard cap underneath the smart shedder.
- **Prometheus + Grafana dashboard.** The stats are inspectable via admin
  API but there's no visualisation.

Any of these can be added as follow-on work; they are intentionally out of
scope for the minimum viable demo.

## Acceptance criteria

The implementation is complete when all five observable behaviours above can
be demonstrated reliably from a fresh `docker compose up --build`, and the
operator can flip between states with the three controller endpoints
(`/shed/on`, `/shed/off`, `/shed/<pct>`) without restarting any service.
