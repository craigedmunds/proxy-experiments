"""
Mock API server that serves example responses from an OpenAPI spec.
Reads openapi.yaml and returns the example values for each endpoint.
"""

import logging
import yaml
from flask import Flask, jsonify, request

app = Flask(__name__)

# Access logging
logging.basicConfig(
    level=logging.INFO,
    format="[mock-api] %(message)s",
)
log = logging.getLogger(__name__)


@app.after_request
def log_request(response):
    log.info(
        '%s %s %s %d',
        request.method,
        request.path,
        request.headers.get("X-Client-Id", "-"),
        response.status_code,
    )
    return response

# Load OAS examples at startup
with open("/app/openapi.yaml") as f:
    spec = yaml.safe_load(f)


def get_example(path: str, method: str, status: str = "200"):
    """Extract example response from the OAS spec."""
    path_spec = spec["paths"].get(path, {})
    method_spec = path_spec.get(method, {})
    responses = method_spec.get("responses", {})
    response_spec = responses.get(status, {})
    content = response_spec.get("content", {})
    json_content = content.get("application/json", {})
    return json_content.get("example")


@app.get("/pets")
def list_pets():
    return jsonify(get_example("/pets", "get")), 200


@app.post("/pets")
def create_pet():
    return jsonify(get_example("/pets", "post", "201")), 201


@app.get("/pets/<int:pet_id>")
def get_pet(pet_id: int):
    example = get_example("/pets/{petId}", "get")
    if example:
        example["id"] = pet_id
    return jsonify(example), 200


@app.delete("/pets/<int:pet_id>")
def delete_pet(pet_id: int):
    return "", 204


@app.get("/pets/<int:pet_id>/vaccinations")
def list_vaccinations(pet_id: int):
    return jsonify(get_example("/pets/{petId}/vaccinations", "get")), 200


@app.get("/health")
def health():
    example = get_example("/health", "get")
    return jsonify(example), 200


if __name__ == "__main__":
    print("[mock-api] starting on :5678", flush=True)
    app.run(host="0.0.0.0", port=5678, threaded=True)
