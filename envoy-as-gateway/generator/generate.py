#!/usr/bin/env python3
"""
Generates envoy.yaml and ratelimit/config.yaml from paths.yaml.

Reads:
    - /input/paths.yaml

Writes:
    - /output/envoy.yaml
    - /output/ratelimit/config.yaml
"""

import yaml
from pathlib import Path

INPUT_DIR = Path("/input")
ENVOY_OUTPUT = Path("/output/envoy")
RATELIMIT_OUTPUT = Path("/output/ratelimit")


def load_paths():
    with open(INPUT_DIR / "paths.yaml") as f:
        return yaml.safe_load(f)


def generate_ratelimit_config(paths: dict) -> dict:
    """Generate the ratelimit service config from paths.yaml."""
    descriptors = []

    for path, config in paths.items():
        for method_conf in config.get("methods", []):
            rate_limit = method_conf.get("rateLimit")
            if not rate_limit:
                continue

            method = method_conf["methodName"]
            descriptor_value = f"{method}:{path}"

            if rate_limit.get("minute"):
                descriptors.append({
                    "key": "path_minute",
                    "value": descriptor_value,
                    "rate_limit": {
                        "unit": "minute",
                        "requests_per_unit": rate_limit["minute"],
                    },
                })

            if rate_limit.get("hour"):
                descriptors.append({
                    "key": "path_hour",
                    "value": descriptor_value,
                    "rate_limit": {
                        "unit": "hour",
                        "requests_per_unit": rate_limit["hour"],
                    },
                })

    return {
        "domain": "api_gateway",
        "descriptors": descriptors,
    }


def generate_envoy_routes(paths: dict) -> list:
    """Generate Envoy route entries from paths.yaml."""
    routes = []

    # Unauthenticated routes first (claim: null)
    for path, config in paths.items():
        methods = config.get("methods", [])
        if all(m.get("claim") is None for m in methods):
            envoy_path = path.split("{")[0].rstrip("/") or "/"
            route = {
                "match": {"prefix": envoy_path if envoy_path != "/" else f"/{path.lstrip('/')}"},
                "route": {"cluster": "app_cluster"},
                "typed_per_filter_config": {
                    "envoy.filters.http.ext_authz": {
                        "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute",
                        "disabled": True,
                    },
                    "envoy.filters.http.ratelimit": {
                        "@type": "type.googleapis.com/envoy.extensions.filters.http.ratelimit.v3.RateLimitPerRoute",
                        "vh_rate_limits": "OVERRIDE",
                        "override_option": "OVERRIDE_POLICY",
                    },
                },
            }
            routes.append(route)

    # Default catch-all route with rate limiting
    routes.append({
        "match": {"prefix": "/"},
        "route": {
            "cluster": "app_cluster",
            "rate_limits": [
                {
                    "actions": [
                        {"request_headers": {"header_name": "x-matched-path", "descriptor_key": "path_minute"}},
                    ],
                },
                {
                    "actions": [
                        {"request_headers": {"header_name": "x-matched-path", "descriptor_key": "path_hour"}},
                    ],
                },
            ],
        },
    })

    return routes


def generate_envoy_config(paths: dict) -> dict:
    """Generate the full envoy.yaml config."""
    routes = generate_envoy_routes(paths)

    return {
        "admin": {
            "address": {
                "socket_address": {"address": "0.0.0.0", "port_value": 9901},
            },
        },
        "static_resources": {
            "listeners": [
                {
                    "name": "ingress",
                    "address": {
                        "socket_address": {"address": "0.0.0.0", "port_value": 8080},
                    },
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                                        "stat_prefix": "ingress_http",
                                        "codec_type": "AUTO",
                                        "access_log": [
                                            {
                                                "name": "envoy.access_loggers.file",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.access_loggers.file.v3.FileAccessLog",
                                                    "path": "/dev/stdout",
                                                    "log_format": {
                                                        "text_format": '[%START_TIME%] "%REQ(:METHOD)% %REQ(X-ENVOY-ORIGINAL-PATH?:PATH)% %PROTOCOL%" %RESPONSE_CODE% %RESPONSE_FLAGS% %BYTES_RECEIVED% %BYTES_SENT% %DURATION% "%REQ(X-CLIENT-ID)%" "%UPSTREAM_HOST%"\n',
                                                    },
                                                },
                                            },
                                        ],
                                        "route_config": {
                                            "name": "local_route",
                                            "virtual_hosts": [
                                                {
                                                    "name": "backend",
                                                    "domains": ["*"],
                                                    "routes": routes,
                                                },
                                            ],
                                        },
                                        "http_filters": [
                                            {
                                                "name": "envoy.filters.http.ext_authz",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
                                                    "transport_api_version": "V3",
                                                    "http_service": {
                                                        "server_uri": {
                                                            "uri": "authz:9002",
                                                            "cluster": "authz_cluster",
                                                            "timeout": "2s",
                                                        },
                                                        "authorization_request": {
                                                            "allowed_headers": {
                                                                "patterns": [
                                                                    {"exact": "authorization"},
                                                                ],
                                                            },
                                                        },
                                                        "authorization_response": {
                                                            "allowed_upstream_headers": {
                                                                "patterns": [
                                                                    {"exact": "x-client-id"},
                                                                    {"exact": "x-matched-path"},
                                                                ],
                                                            },
                                                        },
                                                    },
                                                    "failure_mode_allow": False,
                                                },
                                            },
                                            {
                                                "name": "envoy.filters.http.ratelimit",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.filters.http.ratelimit.v3.RateLimit",
                                                    "domain": "api_gateway",
                                                    "stage": 0,
                                                    "failure_mode_deny": False,
                                                    "rate_limited_as_resource_exhausted": True,
                                                    "enable_x_ratelimit_headers": "DRAFT_VERSION_03",
                                                    "rate_limit_service": {
                                                        "transport_api_version": "V3",
                                                        "grpc_service": {
                                                            "envoy_grpc": {
                                                                "cluster_name": "ratelimit_cluster",
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                            {
                                                "name": "envoy.filters.http.router",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router",
                                                },
                                            },
                                        ],
                                    },
                                },
                            ],
                        },
                    ],
                },
            ],
            "clusters": [
                {
                    "name": "app_cluster",
                    "connect_timeout": "1s",
                    "type": "STRICT_DNS",
                    "lb_policy": "ROUND_ROBIN",
                    "load_assignment": {
                        "cluster_name": "app_cluster",
                        "endpoints": [{"lb_endpoints": [{"endpoint": {"address": {"socket_address": {"address": "app", "port_value": 5678}}}}]}],
                    },
                },
                {
                    "name": "authz_cluster",
                    "connect_timeout": "1s",
                    "type": "STRICT_DNS",
                    "lb_policy": "ROUND_ROBIN",
                    "load_assignment": {
                        "cluster_name": "authz_cluster",
                        "endpoints": [{"lb_endpoints": [{"endpoint": {"address": {"socket_address": {"address": "authz", "port_value": 9002}}}}]}],
                    },
                },
                {
                    "name": "ratelimit_cluster",
                    "connect_timeout": "1s",
                    "type": "STRICT_DNS",
                    "lb_policy": "ROUND_ROBIN",
                    "typed_extension_protocol_options": {
                        "envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                            "@type": "type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions",
                            "explicit_http_config": {
                                "http2_protocol_options": {},
                            },
                        },
                    },
                    "load_assignment": {
                        "cluster_name": "ratelimit_cluster",
                        "endpoints": [{"lb_endpoints": [{"endpoint": {"address": {"socket_address": {"address": "ratelimit", "port_value": 8081}}}}]}],
                    },
                },
            ],
        },
    }


def main():
    paths = load_paths()

    ENVOY_OUTPUT.mkdir(parents=True, exist_ok=True)
    RATELIMIT_OUTPUT.mkdir(parents=True, exist_ok=True)

    # Generate and write envoy.yaml
    envoy_config = generate_envoy_config(paths)
    with open(ENVOY_OUTPUT / "envoy.yaml", "w") as f:
        yaml.dump(envoy_config, f, default_flow_style=False, sort_keys=False)
    print("[generator] wrote /output/envoy/envoy.yaml")

    # Generate and write ratelimit config
    ratelimit_config = generate_ratelimit_config(paths)
    with open(RATELIMIT_OUTPUT / "config.yaml", "w") as f:
        yaml.dump(ratelimit_config, f, default_flow_style=False, sort_keys=False)
    print("[generator] wrote /output/ratelimit/config.yaml")

    # Summary
    print(f"  Paths: {len(paths)}")
    rate_limited = sum(
        1 for p in paths.values()
        for m in p.get("methods", [])
        if m.get("rateLimit")
    )
    print(f"  Rate-limited endpoints: {rate_limited}")
    print(f"  Descriptors: {len(ratelimit_config['descriptors'])}")


if __name__ == "__main__":
    main()
