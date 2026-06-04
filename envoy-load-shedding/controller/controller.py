import threading
import time
from flask import Flask
import requests

ENVOY_ADMIN = "http://envoy:9901"
APP_METRICS = "http://app:5678/metrics"

app = Flask(__name__)


def set_runtime(enforced_pct: int) -> str:
    r = requests.post(
        f"{ENVOY_ADMIN}/runtime_modify",
        params={"shedding.enforced_pct": str(enforced_pct)},
        timeout=2,
    )
    r.raise_for_status()
    return r.text


@app.get("/shed/on")
def shed_on():
    return set_runtime(100), 200


@app.get("/shed/off")
def shed_off():
    return set_runtime(0), 200


@app.get("/shed/<int:pct>")
def shed_pct(pct: int):
    if not 0 <= pct <= 100:
        return "pct must be 0-100", 400
    return set_runtime(pct), 200


@app.get("/health")
def health():
    return "ok", 200


def parse_metric(text: str, name: str) -> str:
    """Extract the value of a prometheus metric by name."""
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            return line.split()[-1]
    return "?"


def poll_metrics():
    """Poll app metrics every 5s, print summary, and control shedding."""
    time.sleep(5)  # let app start
    shedding = False
    while True:
        try:
            r = requests.get(APP_METRICS, timeout=2)
            r.raise_for_status()
            txt = r.text
            inflight = parse_metric(txt, "app_inflight_requests")
            mem_pct = parse_metric(txt, "app_memory_percent")
            cpu_pct = parse_metric(txt, "app_cpu_percent")
            threads = parse_metric(txt, "app_threads")
            reqs = parse_metric(txt, "app_requests_total")

            mem_str = f"{float(mem_pct):.1f}%" if mem_pct != "?" else "?"
            cpu_str = f"{float(cpu_pct):.1f}%" if cpu_pct != "?" else "?"
            threads_val = int(float(threads)) if threads != "?" else 0

            shed_state = "SHEDDING" if shedding else "passing"
            print(
                f"[controller] inflight={inflight} mem={mem_str} "
                f"cpu={cpu_str} threads={threads_val} total_reqs={reqs} [{shed_state}]",
                flush=True,
            )

            # Hysteresis: shed when threads > 150, recover when < 100
            if not shedding and threads_val > 150:
                print("[controller] threads > 150 — enabling shedding", flush=True)
                set_runtime(100)
                shedding = True
            elif shedding and threads_val < 100:
                print("[controller] threads < 100 — disabling shedding", flush=True)
                set_runtime(0)
                shedding = False

        except Exception as e:
            print(f"[controller] metrics error: {e}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    t = threading.Thread(target=poll_metrics, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=8081)
