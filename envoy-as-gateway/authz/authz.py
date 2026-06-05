"""
External authorization service for Envoy ext_authz filter.
Supports two authentication methods:
  1. Basic Auth: client_key as username, client_secret as password
  2. Bearer Token: OAuth-style static bearer tokens

Validates that the client has the required scope/claim for the
requested method+path combination.

On success, sets x-client-id and x-matched-path headers for downstream
rate limiting descriptors.
"""

import base64
import re
import yaml
from flask import Flask, request, Response

app = Flask(__name__)

# Load paths config for scope enforcement
with open("/app/paths.yaml") as f:
    PATHS_CONFIG = yaml.safe_load(f)

# Client credentials database (client_key -> {secret, scopes})
CLIENTS = {
    "ck_a1b2c3d4e5f6g7h8": {
        "secret": "cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4",
        "scopes": ["pets:read", "pets:write"],
    },
    "ck_j9k8l7m6n5o4p3q2": {
        "secret": "cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6",
        "scopes": ["pets:read"],  # read-only client
    },
}

# Bearer token database (token -> client_key)
BEARER_TOKENS = {
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.dGVzdC1jbGllbnQtdG9rZW4": "ck_a1b2c3d4e5f6g7h8",
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.c2Vjb25kLWNsaWVudC10b2tlbg": "ck_j9k8l7m6n5o4p3q2",
}

# Path pattern normalization for rate limit grouping
PATH_PATTERNS = [
    (re.compile(r"^/pets/\d+/vaccinations$"), "/pets/{id}/vaccinations"),
    (re.compile(r"^/pets/\d+$"), "/pets/{id}"),
    (re.compile(r"^/pets$"), "/pets"),
    (re.compile(r"^/health$"), "/health"),
]


def normalize_path(path: str) -> str:
    """Normalize a request path to its pattern for rate limiting."""
    for pattern, normalized in PATH_PATTERNS:
        if pattern.match(path):
            return normalized
    return path


def get_required_claim(normalized_path: str, method: str) -> str | None:
    """Look up the required claim for a method+path from paths.yaml."""
    path_config = PATHS_CONFIG.get(normalized_path)
    if not path_config:
        return None

    for method_conf in path_config.get("methods", []):
        if method_conf["methodName"] == method:
            return method_conf.get("claim")

    return None


def authenticate_basic(auth_header: str) -> str | None:
    """Validate Basic Auth. Returns client_key on success, None on failure."""
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        client_key, client_secret = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return None

    client = CLIENTS.get(client_key)
    if client is None or client["secret"] != client_secret:
        return None
    return client_key


def authenticate_bearer(auth_header: str) -> str | None:
    """Validate Bearer token. Returns client_key on success, None on failure."""
    token = auth_header[7:]  # strip "Bearer "
    return BEARER_TOKENS.get(token)


def client_has_scope(client_key: str, required_claim: str) -> bool:
    """Check if a client has the required scope."""
    client = CLIENTS.get(client_key)
    if not client:
        return False
    return required_claim in client["scopes"]


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def check_auth(path):
    """
    Envoy ext_authz sends the original request to this service.
    We validate auth and scopes, then return 200 (allow) or 403 (deny).
    On allow, we set response headers that Envoy passes upstream.
    """
    auth_header = request.headers.get("Authorization", "")
    method = request.method

    # Authenticate
    client_key = None
    if auth_header.startswith("Basic "):
        client_key = authenticate_basic(auth_header)
    elif auth_header.startswith("Bearer "):
        client_key = authenticate_bearer(auth_header)

    if client_key is None:
        return Response(
            '{"error": "Missing or invalid Authorization header. Use Basic auth (client_key:client_secret) or Bearer token."}',
            status=403,
            content_type="application/json",
        )

    # Determine path and required claim
    original_path = request.headers.get("X-Envoy-Original-Path", f"/{path}")
    normalized = normalize_path(original_path)
    required_claim = get_required_claim(normalized, method)

    # Check scope (null claim means no auth required — shouldn't reach here, but allow)
    if required_claim and not client_has_scope(client_key, required_claim):
        return Response(
            f'{{"error": "Insufficient scope. Required: {required_claim}", "client": "{client_key}"}}',
            status=403,
            content_type="application/json",
        )

    # Auth + scope passed — set headers for rate limiter
    # Rate limit descriptor uses METHOD:path format
    matched_path = f"{method}:{normalized}"

    resp = Response("", status=200)
    resp.headers["x-client-id"] = client_key
    resp.headers["x-matched-path"] = matched_path
    return resp


if __name__ == "__main__":
    print("[authz] starting on :9002", flush=True)
    app.run(host="0.0.0.0", port=9002, threaded=True)
