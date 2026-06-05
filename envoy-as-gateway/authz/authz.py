"""
External authorization service for Envoy ext_authz filter.
Validates Basic Auth (client_id as username, client_secret as password).
On success, sets x-client-id and x-matched-path headers for downstream
rate limiting descriptors.
"""

import base64
import re
from flask import Flask, request, Response

app = Flask(__name__)

# Client credentials database (client_id -> client_secret)
CLIENTS = {
    "ck_a1b2c3d4e5f6g7h8": "cs_z9y8x7w6v5u4t3s2r1q0p9o8n7m6l5k4",
    "ck_j9k8l7m6n5o4p3q2": "cs_f1e2d3c4b5a6z7y8x9w0v1u2t3s4r5q6",
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


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
def check_auth(path):
    """
    Envoy ext_authz sends the original request to this service.
    We validate Basic Auth and return 200 (allow) or 403 (deny).
    On allow, we set response headers that Envoy passes upstream.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Basic "):
        return Response(
            '{"error": "Missing or invalid Authorization header. Use Basic auth with client_id:client_secret."}',
            status=403,
            content_type="application/json",
        )

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        client_id, client_secret = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return Response(
            '{"error": "Malformed Authorization header"}',
            status=403,
            content_type="application/json",
        )

    expected_secret = CLIENTS.get(client_id)
    if expected_secret is None or expected_secret != client_secret:
        return Response(
            '{"error": "Invalid client credentials"}',
            status=403,
            content_type="application/json",
        )

    # Auth passed — set headers for rate limiter
    original_path = request.headers.get("X-Envoy-Original-Path", request.path)
    matched_path = normalize_path(original_path)

    resp = Response("", status=200)
    resp.headers["x-client-id"] = client_id
    resp.headers["x-matched-path"] = matched_path
    return resp


if __name__ == "__main__":
    print("[authz] starting on :9002", flush=True)
    app.run(host="0.0.0.0", port=9002, threaded=True)
