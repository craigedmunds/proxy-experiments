# Envoy as API Gateway: Auth + Scopes + Rate Limiting

A docker-compose stack demonstrating Envoy as an API gateway with:

- **Authentication** — Basic Auth (client_key:client_secret) OR Bearer token (OAuth-style)
- **Scope enforcement** — per-method/path claim requirements
- **Per-minute/hour rate limits** — global per method+path, Redis-backed
- **Config-driven** — `paths.yaml` defines auth rules and rate limits, `oas.yaml` defines the API
- **Generated Envoy config** — `generator/generate.py` runs at startup, producing configs into Docker volumes

## Architecture

```
test-client ──HTTP──▶ envoy:8080 ──HTTP──▶ app:5678
                        │                    (mock API from oas.yaml)
                        │
                        ├──HTTP──▶ authz:9002
                        │          (validates Basic/Bearer auth,
                        │           enforces scopes from paths.yaml,
                        │           sets x-client-id & x-matched-path)
                        │
                        └──gRPC──▶ ratelimit:8081
                                   (rate limit decisions)
                                        │
                                        └──▶ redis:6379
                                             (counter storage)
```

## Quick Start

```bash
# Start the stack (generator runs automatically first)
docker compose up --build
```

The generator container runs first, producing `envoy.yaml` and `ratelimit/config.yaml` into shared volumes. Envoy and the ratelimit service start only after generation completes. The test client then exercises all scenarios.

## Input Files

| File | Purpose |
|---|---|
| `oas.yaml` | OpenAPI spec — defines endpoints and mock response examples |
| `paths.yaml` | Per-path config — required scopes and rate limits |

### paths.yaml format

```yaml
/pets:
  methods:
    - methodName: GET
      claim: pets:read       # required scope (null = no auth)
      rateLimit:
        minute: 5
        hour: 20
    - methodName: POST
      claim: pets:write
      rateLimit:
        minute: 3
        hour: 10
```

## Generated Files (automatic)

The `generator` service runs as an init container before Envoy and ratelimit start.
No manual generation step needed.

| Output | Consumed by |
|---|---|
| `envoy.yaml` | Envoy (via shared volume at `/etc/envoy/`) |
| `ratelimit/config.yaml` | Ratelimit service (via shared volume at `/data/ratelimit/config/`) |

## Manual Testing

```bash
# --- Basic Auth ---

# Full-access client (pets:read + pets:write)
curl -u ck_a1b2c3d4e5f6g7h8:cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4 http://localhost:8080/pets

# Read-only client (pets:read only)
curl -u ck_j9k8l7m6n5o4p3q2:cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6 http://localhost:8080/pets

# Read-only client blocked from writing (403)
curl -X POST -u ck_j9k8l7m6n5o4p3q2:cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6 \
  -H "Content-Type: application/json" -d '{"name":"Rex","species":"dog","age":2}' \
  http://localhost:8080/pets

# Invalid credentials (403)
curl -u ck_a1b2c3d4e5f6g7h8:wrong http://localhost:8080/pets

# --- Bearer Token ---

# Full-access bearer token
curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.dGVzdC1jbGllbnQtdG9rZW4" \
  http://localhost:8080/pets

# Read-only bearer token
curl -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.c2Vjb25kLWNsaWVudC10b2tlbg" \
  http://localhost:8080/pets

# Read-only bearer blocked from writing (403)
curl -X POST \
  -H "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.c2Vjb25kLWNsaWVudC10b2tlbg" \
  -H "Content-Type: application/json" -d '{"name":"Rex","species":"dog","age":2}' \
  http://localhost:8080/pets

# Invalid bearer token (403)
curl -H "Authorization: Bearer invalid_token" http://localhost:8080/pets

# --- No Auth ---

# No auth header (403)
curl http://localhost:8080/pets

# Health check (no auth required)
curl http://localhost:8080/health

# --- Rate Limiting ---

# Hit per-minute limit on GET /pets (limit: 5/min)
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -u ck_a1b2c3d4e5f6g7h8:cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4 \
    http://localhost:8080/pets
done
```

## Authentication

Two methods supported simultaneously. Both resolve to a client identity for logging and scope checks.

### Basic Auth (client_key:client_secret)

| Client Key | Client Secret | Scopes |
|---|---|---|
| `ck_a1b2c3d4e5f6g7h8` | `cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4` | `pets:read`, `pets:write` |
| `ck_j9k8l7m6n5o4p3q2` | `cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6` | `pets:read` |

### Bearer Token (OAuth-style)

| Token | Maps to Client | Scopes |
|---|---|---|
| `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.dGVzdC1jbGllbnQtdG9rZW4` | `ck_a1b2c3d4e5f6g7h8` | `pets:read`, `pets:write` |
| `eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.c2Vjb25kLWNsaWVudC10b2tlbg` | `ck_j9k8l7m6n5o4p3q2` | `pets:read` |

## Rate Limits

**Global** limits per method+path (shared across all clients, regardless of auth method):

| Method | Path | Minute | Hour |
|---|---|---|---|
| GET | `/pets` | 5 | 20 |
| POST | `/pets` | 3 | 10 |
| GET | `/pets/{id}` | 8 | 30 |
| DELETE | `/pets/{id}` | 3 | 10 |
| GET | `/pets/{id}/vaccinations` | 5 | 20 |

Rate limit headers returned in responses:
- `x-ratelimit-limit` — allowed in window
- `x-ratelimit-remaining` — remaining in current window
- `x-ratelimit-reset` — seconds until reset

## Inspecting State

```bash
# Envoy stats
curl localhost:9901/stats?filter=ratelimit
curl localhost:9901/stats?filter=ext_authz

# Redis keys (rate limit counters)
docker compose exec redis redis-cli KEYS '*'

# Watch rate limit counters in real-time
docker compose exec redis redis-cli MONITOR
```

## How It Works

1. **Request arrives** at Envoy on port 8080
2. **ext_authz filter** calls the `authz` service
   - Validates Basic Auth or Bearer token
   - Checks client has the required scope for the method+path (from `paths.yaml`)
   - On success: sets `x-client-id` and `x-matched-path` (e.g. `GET:/pets/{id}`) headers
   - On failure: returns 403
3. **ratelimit filter** sends descriptors to the ratelimit service
   - Descriptors: `path_minute` and `path_hour` with value `METHOD:/path`
   - The ratelimit service checks Redis counters (global, not per-client)
   - Returns OVER_LIMIT (429) or OK
4. **Request is proxied** to the mock API backend

## Configuration

| File | Role |
|---|---|
| `paths.yaml` | Source of truth for auth rules and rate limits |
| `oas.yaml` | OpenAPI spec with mock response examples |
| `generator/generate.py` | Generates configs at startup (runs as init container) |
| `envoy.generated.yaml` | Generated — Envoy gateway config (in Docker volume) |
| `ratelimit/config.yaml` | Generated — rate limit descriptor rules (in Docker volume) |
| `authz/authz.py` | Auth + scope enforcement service |
| `docker-compose.yml` | Stack orchestration |

## Automated Test Client

The test client (`test-client/test_client.py`) runs automatically with `docker compose up` and exercises all scenarios:

### Authentication Tests

| Test | What it does | Expected |
|---|---|---|
| Health check (no auth) | `GET /health` without credentials | 200 |
| Basic auth success | `GET /pets` with valid key:secret | 200 |
| Basic auth failure | `GET /pets` with wrong secret | 403 |
| Bearer token success | `GET /pets` with valid bearer | 200 |
| Bearer token failure | `GET /pets` with invalid bearer | 403 |
| No auth header | `GET /pets` with no Authorization | 403 |

### Scope Tests

| Test | What it does | Expected |
|---|---|---|
| Full-access can write | `POST /pets` and `DELETE /pets/1` with full-access client | 201, 204 |
| Read-only blocked (Basic) | `GET /pets` succeeds, `POST /pets` and `DELETE /pets/1` denied | 200, 403, 403 |
| Read-only blocked (Bearer) | `GET /pets/1` succeeds, `POST /pets` denied via bearer token | 200, 403 |

### Rate Limit Tests

| Test | What it does | Expected |
|---|---|---|
| Per-endpoint limit | 8 rapid `GET /pets` requests (limit 5/min) | First 5 → 200, rest → 429 |
| Independent method limits | 5 rapid `POST /pets` requests (limit 3/min) | First 3 → 201, rest → 429 |
