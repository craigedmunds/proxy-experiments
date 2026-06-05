# Envoy as API Gateway: Auth + Rate Limiting

A docker-compose stack demonstrating Envoy as an API gateway with:

- **Basic Authentication** — client_id as username, client_secret as password
- **Per-minute/hour/day rate limits** — per client, per endpoint
- **Redis-backed rate limiting** — using the official `envoyproxy/ratelimit` service
- **Mock API backend** — serves example responses from an OpenAPI spec

## Architecture

```
test-client ──HTTP──▶ envoy:8080 ──HTTP──▶ app:5678
                        │                    (mock API)
                        │
                        ├──HTTP──▶ authz:9002
                        │          (validates Basic Auth,
                        │           sets x-client-id header)
                        │
                        └──gRPC──▶ ratelimit:8081
                                   (rate limit decisions)
                                        │
                                        └──▶ redis:6379
                                             (counter storage)
```

## Quick Start

```bash
# Start the core stack
docker compose up --build

# In another terminal, run the test client
docker compose run --rm test-client
```

## Manual Testing

```bash
# Authenticated request
curl -u ck_a1b2c3d4e5f6g7h8:cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4 http://localhost:8080/pets

# Unauthenticated (rejected)
curl http://localhost:8080/pets

# Bad credentials (rejected)
curl -u ck_a1b2c3d4e5f6g7h8:wrong http://localhost:8080/pets

# Health check (no auth required)
curl http://localhost:8080/health

# Hit rate limit (send rapidly)
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" -u ck_a1b2c3d4e5f6g7h8:cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4 http://localhost:8080/pets
done
```

## Client Credentials

| Client Key | Client Secret | Notes |
|---|---|---|
| `ck_a1b2c3d4e5f6g7h8` | `cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4` | Primary test client |
| `ck_j9k8l7m6n5o4p3q2` | `cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6` | Secondary client |

## Rate Limits

Limits are set artificially low to easily observe rate limiting.
These are **global** limits per path (shared across all clients):

| Path | Limit | Window |
|---|---|---|
| `/pets` | 5 requests | per minute |
| `/pets/{id}` | 8 requests | per minute |
| `/pets/{id}/vaccinations` | 5 requests | per minute |

Rate limit headers are returned in responses:
- `x-ratelimit-limit` — total allowed in window
- `x-ratelimit-remaining` — remaining in current window
- `x-ratelimit-reset` — seconds until window resets

## API Endpoints (from OpenAPI spec)

| Method | Path | Description |
|---|---|---|
| GET | `/pets` | List all pets |
| POST | `/pets` | Create a pet |
| GET | `/pets/{id}` | Get a pet by ID |
| DELETE | `/pets/{id}` | Delete a pet |
| GET | `/pets/{id}/vaccinations` | List vaccinations for a pet |
| GET | `/health` | Health check (no auth required) |

All endpoints return mock example data defined in `openapi.yaml`.

## Inspecting State

```bash
# Envoy stats
curl localhost:9901/stats?filter=ratelimit
curl localhost:9901/stats?filter=ext_authz

# Redis keys (rate limit counters)
docker compose exec redis redis-cli KEYS '*'

# Watch rate limit counters
docker compose exec redis redis-cli MONITOR
```

## How It Works

1. **Request arrives** at Envoy on port 8080
2. **ext_authz filter** calls the `authz` service with the request
   - Validates Basic Auth credentials
   - On success: returns `x-client-id` and `x-matched-path` headers
   - On failure: returns 403
3. **ratelimit filter** sends descriptors to the `ratelimit` service
   - Descriptors include client_id and matched path
   - The ratelimit service checks Redis counters
   - Returns OVER_LIMIT (429) or OK
4. **Request is proxied** to the mock API backend if all checks pass

## Configuration

- `envoy.yaml` — Gateway configuration (listeners, filters, clusters)
- `ratelimit/config.yaml` — Rate limit rules (domains, descriptors, limits)
- `authz/authz.py` — Client credentials and path normalization
- `openapi.yaml` — API spec with mock response examples
