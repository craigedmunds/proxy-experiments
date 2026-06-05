"""
Integration tests for the Envoy API Gateway.
Tests authentication (Basic + Bearer), scope enforcement, and rate limiting.

Run via: docker compose up --build
Or locally: pytest test_client.py -v
"""

import time

import pytest
import requests
from requests.auth import HTTPBasicAuth

GATEWAY = "http://envoy:8080"

# --- Credentials ---

FULL_ACCESS_BASIC = HTTPBasicAuth("ck_a1b2c3d4e5f6g7h8", "cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4")
READ_ONLY_BASIC = HTTPBasicAuth("ck_j9k8l7m6n5o4p3q2", "cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6")
INVALID_BASIC = HTTPBasicAuth("ck_a1b2c3d4e5f6g7h8", "cs_invalid_secret")

FULL_ACCESS_BEARER = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.dGVzdC1jbGllbnQtdG9rZW4"
READ_ONLY_BEARER = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.c2Vjb25kLWNsaWVudC10b2tlbg"
INVALID_BEARER = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.aW52YWxpZC10b2tlbg"


def bearer_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Fixtures ---

@pytest.fixture(scope="session", autouse=True)
def wait_for_gateway():
    """Wait for the gateway to be ready before running tests."""
    for _ in range(30):
        try:
            resp = requests.get(f"{GATEWAY}/health", timeout=2)
            if resp.status_code == 200:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
    pytest.fail("Gateway not ready after 30s")


# --- Authentication Tests ---

class TestAuthentication:
    """Tests for Basic Auth and Bearer token authentication.
    Uses /pets/1 (8/min limit) for success cases to avoid exhausting /pets quota.
    """

    def test_health_no_auth_required(self):
        resp = requests.get(f"{GATEWAY}/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_basic_auth_valid_full_access(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=FULL_ACCESS_BASIC)
        assert resp.status_code == 200

    def test_basic_auth_valid_read_only(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=READ_ONLY_BASIC)
        assert resp.status_code == 200

    def test_basic_auth_invalid_secret(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=INVALID_BASIC)
        assert resp.status_code == 403

    def test_basic_auth_unknown_key(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=HTTPBasicAuth("unknown", "secret"))
        assert resp.status_code == 403

    def test_bearer_valid_full_access(self):
        resp = requests.get(f"{GATEWAY}/pets/1", headers=bearer_headers(FULL_ACCESS_BEARER))
        assert resp.status_code == 200

    def test_bearer_valid_read_only(self):
        resp = requests.get(f"{GATEWAY}/pets/1", headers=bearer_headers(READ_ONLY_BEARER))
        assert resp.status_code == 200

    def test_bearer_invalid_token(self):
        resp = requests.get(f"{GATEWAY}/pets/1", headers=bearer_headers(INVALID_BEARER))
        assert resp.status_code == 403

    def test_no_auth_header(self):
        resp = requests.get(f"{GATEWAY}/pets/1")
        assert resp.status_code == 403


# --- Scope Tests ---

class TestScopes:
    """Tests for scope/claim enforcement per method+path.
    Uses /pets/{id} (8/min limit) to avoid rate limit interference from auth tests.
    """

    # Full-access client (pets:read + pets:write)

    def test_full_access_can_get_pet(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=FULL_ACCESS_BASIC)
        assert resp.status_code == 200

    def test_full_access_can_post_pets(self):
        resp = requests.post(
            f"{GATEWAY}/pets",
            auth=FULL_ACCESS_BASIC,
            json={"name": "Rex", "species": "dog", "age": 2},
        )
        assert resp.status_code == 201

    def test_full_access_can_delete_pet(self):
        resp = requests.delete(f"{GATEWAY}/pets/1", auth=FULL_ACCESS_BASIC)
        assert resp.status_code == 204

    def test_full_access_can_get_vaccinations(self):
        resp = requests.get(f"{GATEWAY}/pets/1/vaccinations", auth=FULL_ACCESS_BASIC)
        assert resp.status_code == 200

    # Read-only client (pets:read only) via Basic Auth

    def test_read_only_can_get_pet(self):
        resp = requests.get(f"{GATEWAY}/pets/1", auth=READ_ONLY_BASIC)
        assert resp.status_code == 200

    def test_read_only_blocked_post_pets(self):
        resp = requests.post(
            f"{GATEWAY}/pets",
            auth=READ_ONLY_BASIC,
            json={"name": "Rex", "species": "dog", "age": 2},
        )
        assert resp.status_code == 403
        assert "Insufficient scope" in resp.text

    def test_read_only_blocked_delete_pet(self):
        resp = requests.delete(f"{GATEWAY}/pets/1", auth=READ_ONLY_BASIC)
        assert resp.status_code == 403
        assert "Insufficient scope" in resp.text

    # Read-only client via Bearer token

    def test_read_only_bearer_can_get(self):
        resp = requests.get(f"{GATEWAY}/pets/1", headers=bearer_headers(READ_ONLY_BEARER))
        assert resp.status_code == 200

    def test_read_only_bearer_blocked_post(self):
        resp = requests.post(
            f"{GATEWAY}/pets",
            headers=bearer_headers(READ_ONLY_BEARER),
            json={"name": "Rex", "species": "dog", "age": 2},
        )
        assert resp.status_code == 403

    def test_read_only_bearer_blocked_delete(self):
        resp = requests.delete(f"{GATEWAY}/pets/1", headers=bearer_headers(READ_ONLY_BEARER))
        assert resp.status_code == 403


# --- Rate Limit Tests ---

class TestRateLimits:
    """Tests for global per-path rate limiting."""

    def test_get_pets_rate_limited_at_5_per_minute(self):
        """GET /pets has a limit of 5/min. Sending 8 should yield some 429s."""
        statuses = []
        for _ in range(8):
            resp = requests.get(f"{GATEWAY}/pets", auth=FULL_ACCESS_BASIC)
            statuses.append(resp.status_code)
            time.sleep(0.1)

        assert 429 in statuses, f"Expected at least one 429, got: {statuses}"
        assert 200 in statuses, f"Expected at least one 200, got: {statuses}"

    def test_post_pets_rate_limited_at_3_per_minute(self):
        """POST /pets has a limit of 3/min. Sending 6 should yield some 429s."""
        statuses = []
        for _ in range(6):
            resp = requests.post(
                f"{GATEWAY}/pets",
                auth=FULL_ACCESS_BASIC,
                json={"name": "Rex", "species": "dog", "age": 2},
            )
            statuses.append(resp.status_code)
            time.sleep(0.1)

        assert 429 in statuses, f"Expected at least one 429, got: {statuses}"

    def test_rate_limit_returns_headers(self):
        """Rate-limited responses should include x-ratelimit-* headers."""
        resp = requests.get(f"{GATEWAY}/pets/1", auth=FULL_ACCESS_BASIC)
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers

    def test_rate_limit_shared_across_clients(self):
        """Rate limit is global — both clients share the same counter."""
        # Use a less-tested path to get a fresh counter window
        statuses = []
        for i in range(8):
            # Alternate between the two clients
            auth = FULL_ACCESS_BASIC if i % 2 == 0 else READ_ONLY_BASIC
            resp = requests.get(f"{GATEWAY}/pets/1/vaccinations", auth=auth)
            statuses.append(resp.status_code)
            time.sleep(0.05)

        assert 429 in statuses, f"Expected rate limit hit across clients, got: {statuses}"
