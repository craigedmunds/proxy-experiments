# Load-Shedding Demo: Runtime-Controlled Envoy Filter

A docker-compose stack demonstrating runtime-controlled load shedding via
Envoy's `local_ratelimit` filter, toggled by an external HTTP controller.

## Architecture

```
loadgen ──HTTP──▶ envoy:8080 ──HTTP──▶ app:5678
                     ▲
                     │ POST /runtime_modify
                     │
                  controller:8081 ◀──HTTP── operator (curl)
```

## Quick Start

```bash
docker compose up --build
```

This starts all four services. `loadgen` immediately begins sending 50 RPS
through Envoy to the backend.

## Driving the Demo

In a second terminal:

```bash
# Check baseline — all requests passing through
curl 'localhost:9901/stats?filter=shedding'

# Full shed — all new requests get 429
curl -X POST localhost:8081/shed/on

# Graduated shed — ~50% of requests get 429
curl -X POST localhost:8081/shed/50

# Recovery — all requests pass again
curl -X POST localhost:8081/shed/off
```

## How It Works

The Envoy `local_ratelimit` filter is configured with a **zero-token bucket**
(no static rate). Shedding is controlled entirely by the runtime
`filter_enforced` percentage:

- `enforced_pct = 0` → all requests pass (default)
- `enforced_pct = 100` → all requests rejected with 429
- `enforced_pct = N` → ~N% of requests rejected

The controller writes to Envoy's admin `/runtime_modify` endpoint to change
`shedding.enforced_pct` at runtime. No reload, no restart — takes effect on
the next request.

## Key Design Points

1. **Zero-token bucket** — no static sustainable rate; behaviour is purely
   runtime-driven.
2. **`filter_enabled` at 100%** — filter always evaluates (shadow mode is free
   for metrics).
3. **`filter_enforced` at 0%** — baseline is full pass-through.
4. **`admin_layer` in `layered_runtime`** — required for `/runtime_modify` to
   take effect.
5. **429 status code** — explicit even though it's the default.
6. **In-flight safety** — requests already past the filter complete normally
   regardless of subsequent state changes.

## Inspecting Envoy State

| Endpoint | Purpose |
|---|---|
| `GET localhost:9901/runtime` | Current runtime values and layer of origin |
| `GET localhost:9901/stats?filter=shedding` | Filter counters (ok, rate_limited) |
| `GET localhost:9901/stats/prometheus` | All stats in Prometheus format |
| `GET localhost:9901/config_dump` | Full effective config |

## Testing In-Flight Safety

To demonstrate that in-flight requests are unaffected by runtime changes,
swap the backend for a slow responder:

```yaml
# In docker-compose.yml, replace the app service with:
  app:
    image: kennethreitz/httpbin
    networks: [demo]
```

Then point loadgen at `http://envoy:8080/delay/3` and toggle shedding
mid-stream. Requests already in flight will complete with 200 after their
~3s delay, while new requests immediately get 429.

## Controller Endpoints

| Method | Path | Effect |
|---|---|---|
| POST | `/shed/on` | Set enforcement to 100% (full shed) |
| POST | `/shed/off` | Set enforcement to 0% (pass-through) |
| POST | `/shed/<pct>` | Set enforcement to given percentage (0-100) |
| GET | `/health` | Health check |
