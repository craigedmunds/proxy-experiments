import time
import threading
import os
import resource
from flask import Flask, request
from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = Flask(__name__)

# Metrics
REQUEST_COUNT = Counter("app_requests_total", "Total requests", ["endpoint", "status"])
INFLIGHT = Gauge("app_inflight_requests", "Currently in-flight requests")
REQUEST_DURATION = Histogram(
    "app_request_duration_seconds", "Request duration", ["endpoint"]
)
MEMORY_RSS = Gauge("app_memory_rss_bytes", "Resident set size in bytes")
MEMORY_LIMIT = Gauge("app_memory_limit_bytes", "Memory limit in bytes")
MEMORY_PCT = Gauge("app_memory_percent", "Memory usage as percentage of limit")
CPU_USER = Gauge("app_cpu_user_seconds", "User CPU time")
CPU_SYSTEM = Gauge("app_cpu_system_seconds", "System CPU time")
CPU_PCT = Gauge("app_cpu_percent", "CPU usage percentage (since last sample)")
THREADS = Gauge("app_threads", "Active thread count")

# State for CPU % calculation
_last_cpu_time = None
_last_wall_time = None


def update_process_metrics():
    """Update process-level metrics."""
    global _last_cpu_time, _last_wall_time

    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_total = usage.ru_utime + usage.ru_stime
    wall_now = time.time()

    CPU_USER.set(usage.ru_utime)
    CPU_SYSTEM.set(usage.ru_stime)

    # CPU %
    if _last_cpu_time is not None:
        cpu_delta = cpu_total - _last_cpu_time
        wall_delta = wall_now - _last_wall_time
        if wall_delta > 0:
            CPU_PCT.set((cpu_delta / wall_delta) * 100.0)
    _last_cpu_time = cpu_total
    _last_wall_time = wall_now

    # Memory
    rss = usage.ru_maxrss * 1024  # KB -> bytes
    MEMORY_RSS.set(rss)
    THREADS.set(threading.active_count())

    # Memory limit: try cgroup (container), fall back to total system RAM
    mem_limit = _get_memory_limit()
    MEMORY_LIMIT.set(mem_limit)
    if mem_limit > 0:
        MEMORY_PCT.set((rss / mem_limit) * 100.0)


def _get_memory_limit():
    """Get container memory limit from cgroup, or total system RAM."""
    # cgroup v2
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            val = f.read().strip()
            if val != "max":
                return int(val)
    except (FileNotFoundError, ValueError):
        pass
    # cgroup v1
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            val = int(f.read().strip())
            # kernel reports a huge number if unlimited
            if val < 2**62:
                return val
    except (FileNotFoundError, ValueError):
        pass
    # fallback: total RAM
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024  # KB -> bytes
    except FileNotFoundError:
        pass
    return 0


@app.get("/delay/<int:seconds>")
@INFLIGHT.track_inprogress()
def delay(seconds: int):
    # print(f"[app] req in", flush=True)
    with REQUEST_DURATION.labels(endpoint="/delay").time():
        time.sleep(seconds)
    # print(f"[app] req out", flush=True)
    REQUEST_COUNT.labels(endpoint="/delay", status="200").inc()
    return "ok\n", 200


@app.get("/")
@INFLIGHT.track_inprogress()
def root():
    # print("[app] req", flush=True)
    REQUEST_COUNT.labels(endpoint="/", status="200").inc()
    return "ok\n", 200


@app.get("/metrics")
def metrics():
    update_process_metrics()
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    print("[app] starting on :5678", flush=True)
    app.run(host="0.0.0.0", port=5678, threaded=True)
