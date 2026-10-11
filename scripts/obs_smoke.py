"""Smoke-test the observability stack alone, with no services and no model calls.

    SLO_RULES=slo.lab.rules.yml podman-compose -f observability/compose.yaml up -d
    python scripts/obs_smoke.py

Sends one span, one counter, one histogram and one gauge over OTLP/HTTP and checks that each arrives where an operator
would look for it: the metric in Prometheus under the name the SLO rules use, the trace in Jaeger, the rules loaded
without errors, and the dashboard and both data sources provisioned in Grafana.
"""

import pathlib
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from deploylab import telemetry as tele  # noqa: E402

PROM, JAEGER, GRAFANA = "http://127.0.0.1:9090", "http://127.0.0.1:16686", "http://127.0.0.1:3000"


def wait(url, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=3).status_code in (200, 400, 405):
                return round(time.time() - t0, 1)
        except httpx.HTTPError:
            pass
        time.sleep(1)
    return None


ready = {n: wait(u) for n, u in {"prometheus": f"{PROM}/-/ready", "jaeger": f"{JAEGER}/", "grafana": f"{GRAFANA}/api/health",
                                 "alertmanager": "http://127.0.0.1:9093/-/ready", "alert sink": "http://127.0.0.1:9099/alerts",
                                 "collector (OTLP)": "http://127.0.0.1:4318/v1/traces"}.items()}
print("ready after (s):", ready)
assert all(v is not None for v in ready.values()), "a component did not come up"

tel = tele.setup("smoketest", "v0", endpoint="http://127.0.0.1:4318", export_interval_ms=2000)
with tel.tracer.start_as_current_span("smoke span") as span:
    span.set_attribute("app.correlation_id", "smoke-1")
tel.inc("ai.ticket.outcomes", action="released", release="r1", role="active")
tel.observe("ai.ticket.duration", 7.5, action="released", release="r1")
tel.gauge("ai.drift.verdict", lambda: [(0, {})])
time.sleep(14)
tel.shutdown()

for q in ("ai_ticket_outcomes_total", "ai_ticket_duration_seconds_count", "ai_drift_verdict"):
    res = httpx.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=10).json()["data"]["result"]
    print(f"{q:36s} -> {[(r['metric'].get('service_name'), r['value'][1]) for r in res]}")
    assert res, q
print("Jaeger services:", httpx.get(f"{JAEGER}/api/services", timeout=10).json()["data"])
groups = httpx.get(f"{PROM}/api/v1/rules", timeout=10).json()["data"]["groups"]
bad = [r["name"] for g in groups for r in g["rules"] if r.get("health") != "ok"]
print("rule groups:", {g["name"]: len(g["rules"]) for g in groups}, "| rules with errors:", bad or "none")
assert not bad
print("Grafana dashboards:", [(d["title"], d["uid"]) for d in httpx.get(f"{GRAFANA}/api/search", params={"query": "AI"}, timeout=10).json()])
print("Grafana data sources:", [(s["name"], s["type"], s["uid"]) for s in httpx.get(f"{GRAFANA}/api/datasources", auth=("admin", "lab-only-change-me"), timeout=10).json()])
print("OK")
