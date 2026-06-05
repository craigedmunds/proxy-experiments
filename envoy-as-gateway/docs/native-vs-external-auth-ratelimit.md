# Native vs External Service: Auth & Rate Limiting in Envoy

## Current Architecture (External Services)

Our implementation delegates auth and rate limiting to separate microservices:

```
envoy:8080
  ├── ext_authz HTTP → authz:9002 (Flask, validates Basic/Bearer, enforces scopes)
  └── ratelimit gRPC → ratelimit:8081 (envoyproxy/ratelimit + Redis)
```

This adds 3 extra containers (authz, ratelimit, redis) beyond Envoy itself.

---

## Is Delegating the Only Way?

No. Envoy has several native mechanisms for both auth and rate limiting that can run
entirely within the proxy process, without external services. The tradeoffs are around
flexibility, distributed coordination, and operational complexity.

---

## Native Alternatives

### 1. Envoy's Built-in Basic Auth Filter

Since Envoy v1.26+, there's a native `envoy.filters.http.basic_auth` filter.

**What it does:**
- Validates Basic Auth credentials against an htpasswd-format user list
- Runs inline in the filter chain — zero network calls
- Sets `x-]username` header on success for downstream filters

**What it doesn't do:**
- No Bearer token support
- No scope/claim enforcement (it's username:password only)
- No custom logic — just credential validation

**Config example:**
```yaml
http_filters:
  - name: envoy.filters.http.basic_auth
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.basic_auth.v3.BasicAuth
      users:
        inline_string: "client_a:{SHA}abc123...\nclient_b:{SHA}def456...\n"
```

**Verdict:** Suitable for simple username/password gating. Cannot replace our authz
service because we need scope enforcement and Bearer token support.

---

### 2. Lua Filter (envoy.filters.http.lua)

Envoy embeds LuaJIT and allows inline Lua scripts to run in the request/response path.

**What it can do:**
- Full request inspection (headers, path, method, body)
- Credential validation (decode Basic Auth, look up Bearer tokens)
- Scope enforcement (match method+path against a rules table)
- Set/modify headers (e.g., x-client-id, x-matched-path)
- Return immediate responses (403, 429)
- Make HTTP calls to other services (httpCall)
- Implement token-bucket rate limiting with shared state via `streamInfo:dynamicMetadata`

**Lua auth + scopes example:**
```yaml
http_filters:
  - name: envoy.filters.http.lua
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua
      default_source_code:
        inline_string: |
          -- Client database
          local clients = {
            ["ck_a1b2c3d4e5f6g7h8"] = {
              secret = "cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4",
              scopes = {["pets:read"]=true, ["pets:write"]=true}
            },
            ["ck_j9k8l7m6n5o4p3q2"] = {
              secret = "cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6",
              scopes = {["pets:read"]=true}
            }
          }

          -- Bearer token mapping
          local tokens = {
            ["eyJhbGci...dG9rZW4"] = "ck_a1b2c3d4e5f6g7h8",
            ["eyJhbGci...b2tlbg"]  = "ck_j9k8l7m6n5o4p3q2"
          }

          -- Scope requirements (method:path -> required scope)
          local scope_rules = {
            ["GET:/pets"]     = "pets:read",
            ["POST:/pets"]    = "pets:write",
            ["DELETE:/pets/"] = "pets:write",
          }

          function envoy_on_request(handle)
            local path = handle:headers():get(":path")
            local method = handle:headers():get(":method")
            local auth = handle:headers():get("authorization") or ""

            -- Skip auth for health
            if path == "/health" then return end

            -- Authenticate
            local client_key = nil
            if auth:sub(1,6) == "Basic " then
              local decoded = handle:base64Decode(auth:sub(7))
              local key, secret = decoded:match("([^:]+):(.+)")
              local client = clients[key]
              if client and client.secret == secret then
                client_key = key
              end
            elseif auth:sub(1,7) == "Bearer " then
              client_key = tokens[auth:sub(8)]
            end

            if not client_key then
              handle:respond({[":status"] = "403"}, "Forbidden")
              return
            end

            -- Check scope
            local required = scope_rules[method .. ":" .. path]
            if required and not clients[client_key].scopes[required] then
              handle:respond({[":status"] = "403"}, "Insufficient scope")
              return
            end

            -- Set headers for downstream
            handle:headers():add("x-client-id", client_key)
          end
```

**Limitations:**
- LuaJIT runs single-threaded per worker — complex logic adds latency
- No persistent state across requests (each request is isolated)
- No shared counters for rate limiting without external calls
- Debugging is painful (no debugger, limited logging)
- Large scripts become unreadable in YAML
- Lua's string handling/base64 is basic (no native JWT parsing)

**Verdict:** Can fully replace our authz service for auth + scope enforcement.
The logic is simple enough for Lua. Rate limiting is the harder problem (see below).

---

### 3. Local Rate Limit Filter (envoy.filters.http.local_ratelimit)

Envoy has a built-in token-bucket rate limiter that runs entirely in-process.

**What it does:**
- Per-route or global token bucket rate limiting
- Configurable fill rate and max tokens
- Response headers (x-ratelimit-limit, x-ratelimit-remaining, x-ratelimit-reset)
- Can be applied per-route via `typed_per_filter_config`

**Config example (per-route):**
```yaml
routes:
  - match:
      prefix: "/pets"
      headers:
        - name: ":method"
          exact_match: "GET"
    route:
      cluster: app_cluster
    typed_per_filter_config:
      envoy.filters.http.local_ratelimit:
        "@type": type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
        stat_prefix: pets_get
        token_bucket:
          max_tokens: 5
          tokens_per_fill: 5
          fill_interval: 60s
        filter_enabled:
          runtime_key: local_rate_limit_enabled
          default_value:
            numerator: 100
            denominator: HUNDRED
        filter_enforced:
          runtime_key: local_rate_limit_enforced
          default_value:
            numerator: 100
            denominator: HUNDRED
        response_headers_to_add:
          - append_action: OVERWRITE_IF_EXISTS_OR_ADD
            header:
              key: x-ratelimit-limit
              value: "5"
```

**Key difference from global rate limiting:**
- **Local** = per Envoy instance. If you have 3 Envoy replicas, each gets its own bucket.
- **Global** (our current approach) = shared across all instances via external service + Redis.

**Verdict:** Perfect replacement for our ratelimit service IF we only run a single
Envoy instance (which we do in this experiment). Falls apart in multi-instance deployments
where you need coordinated limits.

---

### 4. Wasm Filter (envoy.filters.http.wasm)

Write filters in Rust, Go, C++, or AssemblyScript, compile to WebAssembly, run in Envoy.

**What it can do:**
- Everything Lua can do, but with full language ecosystems
- JWT parsing/validation (with proper crypto libraries)
- Complex auth logic
- HTTP callouts to external services
- Shared state across requests (via shared queues/KV)

**Advantages over Lua:**
- Real languages with proper tooling
- Better performance for complex logic
- Type safety, unit testable
- Proper JWT/crypto support (Rust has good crates)
- Can maintain in-process state (shared data)

**Disadvantages:**
- Compilation step required (Rust → .wasm)
- Larger operational overhead (build pipeline, artifact management)
- Debugging Wasm is still immature
- proxy-wasm SDK has a learning curve
- Adds latency if doing crypto in Wasm vs delegating to a native filter

**Verdict:** Best option if we outgrow Lua but want to stay in-process. Overkill
for this experiment's complexity level.

---

### 5. JWT Authentication Filter (envoy.filters.http.jwt_authn)

Native filter for validating JWTs against JWKS endpoints or inline keys.

**What it does:**
- Validates JWT signatures (RS256, ES256, etc.)
- Checks expiration, issuer, audience claims
- Extracts claims and forwards them as headers or metadata
- Fetches JWKS from remote endpoints

**What it doesn't do:**
- No Basic Auth
- No scope enforcement (only validation)
- Cannot do custom credential lookups

**Config example:**
```yaml
http_filters:
  - name: envoy.filters.http.jwt_authn
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.jwt_authn.v3.JwtAuthentication
      providers:
        my_provider:
          issuer: "https://auth.example.com"
          audiences: ["api.example.com"]
          local_jwks:
            inline_string: '{"keys":[...]}'
          forward_payload_header: x-jwt-payload
      rules:
        - match: {prefix: "/pets"}
          requires: {provider_name: "my_provider"}
        - match: {prefix: "/health"}
```

**Verdict:** Would replace Bearer token validation if we used real JWTs. Doesn't
cover Basic Auth or scope enforcement.

---

## Comparison Matrix

| Capability | ext_authz + ratelimit (current) | Lua Filter | Local Rate Limit | Wasm | Native Filters |
|---|---|---|---|---|---|
| Basic Auth | ✅ | ✅ | — | ✅ | ✅ (basic_auth filter) |
| Bearer/JWT validation | ✅ | ✅ (limited) | — | ✅ | ✅ (jwt_authn filter) |
| Scope enforcement | ✅ | ✅ | — | ✅ | ❌ |
| Per-path rate limiting | ✅ | ⚠️ (no shared state) | ✅ (per instance) | ⚠️ | ✅ (per instance) |
| Global rate limiting (multi-instance) | ✅ | ❌ | ❌ | ❌ | ❌ |
| Zero extra containers | ❌ (3 extra) | ✅ | ✅ | ✅ | ✅ |
| Latency | +2-5ms (network hops) | +0.1-0.5ms | +0.01ms | +0.1-1ms | +0.01ms |
| Operational complexity | High (deploy, monitor, scale) | Low | Low | Medium | Low |
| Testability | High (unit test Python) | Low (manual) | N/A (config) | Medium | N/A (config) |
| Hot reload | Restart container | Restart Envoy | Runtime keys | Reload .wasm | Restart Envoy |
| Custom logic flexibility | Unlimited | Good | None | Unlimited | None |

---

## Recommended Approach for This Experiment

### Option A: Lua for Auth + Local Rate Limit (Simplest Native)

Replace both external services with native Envoy filters:

```
envoy:8080 (Lua filter for auth + local_ratelimit filter)
  └── app:5678
```

**Stack:** 2 containers (envoy + app) instead of 6.

**Trade-offs:**
- ✅ Dramatically simpler deployment
- ✅ Lower latency (no network hops for auth/ratelimit)
- ✅ No Redis dependency
- ⚠️ Rate limits are per-instance (fine for single instance)
- ⚠️ Lua code in YAML is harder to test/maintain
- ❌ Won't scale to multi-instance without going back to external ratelimit

### Option B: Lua for Auth + Keep External Rate Limit (Hybrid)

Replace only the authz service with Lua, keep the global ratelimit service:

```
envoy:8080 (Lua filter for auth)
  ├── app:5678
  └── ratelimit:8081 + redis:6379
```

**Stack:** 4 containers instead of 6.

**Trade-offs:**
- ✅ Removes one service while keeping global rate limiting
- ✅ Auth is fast (in-process)
- ✅ Rate limits work correctly across multiple instances
- ⚠️ Still need Redis + ratelimit service

### Option C: Keep Current Architecture (Production-Ready)

Our current approach is the canonical Envoy pattern for production:

**Trade-offs:**
- ✅ Battle-tested pattern (widely used in production)
- ✅ Auth service is independently testable and deployable
- ✅ Global rate limiting works across any number of instances
- ✅ Easy to extend (add OAuth, OIDC, LDAP, etc.)
- ❌ More containers to manage
- ❌ Higher latency per request

---

## When to Use What

| Scenario | Recommendation |
|---|---|
| Single-instance gateway, simple auth | **Lua + Local Rate Limit** |
| Single-instance, needs real JWT validation | **jwt_authn + Local Rate Limit** |
| Multi-instance, shared rate limits needed | **External ratelimit + Redis** (current) |
| Complex auth (OIDC, LDAP, dynamic clients) | **ext_authz** (current) |
| High performance, complex logic | **Wasm filter** |
| Experimentation / learning | **Lua** (fastest iteration) |
| Production API gateway | **ext_authz + external ratelimit** (current) |

---

## Next Steps / Experiment Ideas

1. **Implement Option A** as a `docker-compose.lua.yml` variant for comparison
2. **Benchmark** latency: Lua+local vs ext_authz+external at various RPS
3. **Try jwt_authn** with real RS256 JWTs instead of static Bearer tokens
4. **Wasm prototype** in Rust for auth+scope (explore proxy-wasm SDK)
5. **Combine** local + global rate limiting (local as first-pass protection, global for accurate limits)

---

## References

- [Envoy Lua Filter docs](https://www.envoyproxy.io/docs/envoy/v1.31.3/configuration/http/http_filters/lua_filter)
- [Envoy Local Rate Limit Filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/local_rate_limit_filter)
- [Envoy Basic Auth Filter](https://www.envoyproxy.io/docs/envoy/v1.30.11/configuration/http/http_filters/basic_auth_filter.html)
- [Envoy JWT Authentication Filter](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/jwt_authn_filter)
- [Envoy Wasm Filter](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/advanced/wasm)
- [Envoy Global Rate Limiting](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/other_features/global_rate_limiting.html)
- [Envoy Gateway Rate Limit Design](https://gateway.envoyproxy.io/v0.5/design/rate-limit/)
- [proxy-wasm spec](https://github.com/proxy-wasm/spec)
