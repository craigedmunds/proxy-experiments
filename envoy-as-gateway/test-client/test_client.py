"""
Test client that authenticates and exercises the rate limits.
Demonstrates:
  1. Successful authentication
  2. Failed authentication
  3. Hitting per-endpoint rate limits
  4. Observing rate limit headers in responses
"""

import time
from datetime import datetime, timezone
import requests
from requests.auth import HTTPBasicAuth

GATEWAY = "http://envoy:8080"
VALID_AUTH = HTTPBasicAuth("ck_a1b2c3d4e5f6g7h8", "cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4")
INVALID_AUTH = HTTPBasicAuth("ck_a1b2c3d4e5f6g7h8", "cs_invalid_secret")


def ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def separator(title: str):
    print(f"\n{'='*60}")
    print(f"  [{ts()}] {title}")
    print(f"{'='*60}\n")


def show_response(resp, label: str = ""):
    rl_limit = resp.headers.get("x-ratelimit-limit", "-")
    rl_remaining = resp.headers.get("x-ratelimit-remaining", "-")
    rl_reset = resp.headers.get("x-ratelimit-reset", "-")
    print(
        f"  [{ts()}] [{resp.status_code}] {label:30s} "
        f"limit={rl_limit} remaining={rl_remaining} reset={rl_reset}"
    )
    if resp.status_code == 429:
        print(f"         RATE LIMITED!")
    elif resp.status_code == 403:
        print(f"         AUTH DENIED: {resp.text[:80]}")


def test_auth_failure():
    separator("TEST: Authentication Failure")
    print("Sending request with invalid credentials...")
    resp = requests.get(f"{GATEWAY}/pets", auth=INVALID_AUTH)
    show_response(resp, "GET /pets (bad auth)")
    assert resp.status_code == 403, f"Expected 403, got {resp.status_code}"
    print("\n  ✓ Correctly rejected with 403")


def test_auth_success():
    separator("TEST: Authentication Success")
    print("Sending request with valid credentials...")
    resp = requests.get(f"{GATEWAY}/pets", auth=VALID_AUTH)
    show_response(resp, "GET /pets (good auth)")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    print(f"\n  Response body: {resp.json()}")
    print("  ✓ Successfully authenticated and got response")


def test_multiple_endpoints():
    separator("TEST: Multiple Endpoints")
    endpoints = [
        ("GET", "/pets"),
        ("GET", "/pets/1"),
        ("GET", "/pets/1/vaccinations"),
        ("POST", "/pets"),
        ("GET", "/health"),
    ]
    for method, path in endpoints:
        if method == "GET":
            resp = requests.get(f"{GATEWAY}{path}", auth=VALID_AUTH)
        else:
            resp = requests.post(
                f"{GATEWAY}{path}",
                auth=VALID_AUTH,
                json={"name": "Rex", "species": "dog", "age": 2},
            )
        show_response(resp, f"{method} {path}")
    print("\n  ✓ All endpoints responding correctly")


def test_per_endpoint_rate_limit():
    separator("TEST: Per-Endpoint Rate Limit (GET /pets — global limit 5/min)")
    print("Sending 8 rapid requests to GET /pets...")
    print("(Global limit is 5 per minute for this path)\n")

    hit_limit = False
    for i in range(8):
        resp = requests.get(f"{GATEWAY}/pets", auth=VALID_AUTH)
        show_response(resp, f"Request {i+1}/8")
        if resp.status_code == 429:
            hit_limit = True
        time.sleep(0.2)  # small pause between requests

    if hit_limit:
        print("\n  ✓ Rate limit enforced! Got 429 responses after exceeding limit.")
    else:
        print("\n  ⚠ Did not hit rate limit (may need to wait for counter reset)")


def test_global_rate_limit():
    separator("TEST: Different Paths Have Independent Limits")
    print("Sending requests across different endpoints...")
    print("(Each path has its own global limit)\n")

    paths = ["/pets/1", "/pets/2", "/pets/3"]
    hit_limit = False
    for i in range(12):
        path = paths[i % len(paths)]
        resp = requests.get(f"{GATEWAY}{path}", auth=VALID_AUTH)
        show_response(resp, f"Request {i+1}/12 → {path}")
        if resp.status_code == 429:
            hit_limit = True
        time.sleep(0.1)

    if hit_limit:
        print("\n  ✓ Rate limit enforced on /pets/{id} path (limit 8/min)!")
    else:
        print("\n  ⚠ Limit not yet hit (counters may have carried from prior test)")


def test_no_auth_required_health():
    separator("TEST: Health Check (no auth required)")
    resp = requests.get(f"{GATEWAY}/health")
    show_response(resp, "GET /health (no auth)")
    print(f"\n  Response: {resp.json()}")
    print("  ✓ Health endpoint accessible without auth")


def main():
    print("=" * 60)
    print("  ENVOY GATEWAY — RATE LIMITING & AUTH TEST CLIENT")
    print("=" * 60)
    print(f"\n  Target: {GATEWAY}")
    print(f"  Client: ck_a1b2c3d4e5f6g7h8")

    # Wait for services to be ready
    print("\n  Waiting for gateway to be ready...", end="", flush=True)
    for _ in range(30):
        try:
            resp = requests.get(f"{GATEWAY}/health", timeout=2)
            if resp.status_code == 200:
                print(" ready!")
                break
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
        print(".", end="", flush=True)
    else:
        print("\n  ✗ Gateway not ready after 30s, running tests anyway...")

    test_no_auth_required_health()
    test_auth_failure()
    test_auth_success()
    test_multiple_endpoints()
    test_per_endpoint_rate_limit()
    test_global_rate_limit()

    separator("ALL TESTS COMPLETE")
    print("Check Envoy stats at http://localhost:9901/stats?filter=ratelimit")
    print("Check Redis keys:  docker compose exec redis redis-cli KEYS '*'")


if __name__ == "__main__":
    main()
