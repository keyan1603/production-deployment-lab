"""Observability and SLOs on a live system: one trace across services, the metrics behind the SLOs, and alerts that fire and clear.

    python -u scripts/phase3_study.py

Starts the open-source observability stack in Podman (OpenTelemetry Collector, Jaeger, Prometheus with the generated
SLO rules at minute-scale windows, Alertmanager, a webhook sink, Grafana), then the services on the host with the
synthetic-probe monitor, and checks what an operator would rely on. The incident is staged by activating two bad
releases (one for tickets, one for the knowledge assistant) WITHOUT the gate, to see whether the signals notice.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx
from tabulate import tabulate

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.stack import ROOT, Stack, load_gemini_key  # noqa: E402
from scripts.save_baseline import calibrate  # noqa: E402
from tickets.data import TICKETS  # noqa: E402

PROM, JAEGER, GRAFANA, ALERTM, SINK = ("http://127.0.0.1:9090", "http://127.0.0.1:16686", "http://127.0.0.1:3000",
                                      "http://127.0.0.1:9093", "http://127.0.0.1:9099")
OTLP = "http://127.0.0.1:4318"
COMPOSE = [os.environ.get("PODMAN_COMPOSE", "podman-compose"), "-f", "observability/compose.yaml"]
NORMAL = [t for t in TICKETS if t.kind == "normal"]
T0 = time.time()


def banner(text):
    print(f"\n{'=' * 8} {text}")


def stamp():
    return f"+{time.time() - T0:5.0f}s"


def prom(query):
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": query}, timeout=15).json()
    return r["data"]["result"] if r.get("status") == "success" else []


def wait(url, ok=lambda r: r.status_code == 200, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if ok(httpx.get(url, timeout=3)):
                return round(time.time() - t0, 1)
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{url} did not become ready")


class Ops:
    def __init__(self, stack):
        self.s, self.h = stack, {"X-Admin-Key": stack.admin_key}

    def login(self, user, aud="ticket-agent"):
        return httpx.post(f"{self.s.urls['identity']}/login", json={"username": user, "audience": aud}, timeout=10).json()["token"]

    def admin(self, service, path, body=None):
        url = f"{self.s.urls[service]}/admin/releases{path}"
        r = httpx.post(url, json=body or {}, headers=self.h, timeout=120) if body is not None else httpx.get(url, headers=self.h, timeout=20)
        return r.status_code, r.json()

    def ticket(self, tid, text):
        t = time.time()
        r = httpx.post(f"{self.s.urls['tickets']}/tickets", json={"id": tid, "text": text}, timeout=300,
                       headers={"Authorization": f"Bearer {self.login('sam')}", "X-Correlation-Id": f"p3-{tid}"})
        b = r.json()
        return {"id": tid, "http": r.status_code, "action": b.get("action"), "release": (b.get("release") or {}).get("id"), "seconds": round(time.time() - t, 1)}


def firing():
    alerts = httpx.get(f"{PROM}/api/v1/alerts", timeout=10).json()["data"]["alerts"]
    return {a["labels"]["alertname"] for a in alerts if a["state"] == "firing"}


def main():
    key = load_gemini_key()
    env = {**os.environ, "SLO_RULES": "slo.lab.rules.yml"}
    banner("1. the observability stack (all open source) in Podman")
    subprocess.run(COMPOSE + ["down"], capture_output=True, text=True, env=env)
    up = subprocess.run(COMPOSE + ["up", "-d"], capture_output=True, text=True, env=env)
    if up.returncode:
        raise SystemExit(up.stderr[-600:])
    ready = {"prometheus": wait(f"{PROM}/-/ready"), "alertmanager": wait(f"{ALERTM}/-/ready"), "jaeger": wait(f"{JAEGER}/"),
             "grafana": wait(f"{GRAFANA}/api/health"), "alert sink": wait(f"{SINK}/alerts"),
             "collector": wait(f"{OTLP}/v1/traces", ok=lambda r: r.status_code in (200, 400, 405))}
    print("ready after (s):", ready)
    rules = httpx.get(f"{PROM}/api/v1/rules", timeout=10).json()["data"]["groups"]
    print("Prometheus loaded", {g["name"]: len(g["rules"]) for g in rules}, "| rule errors:", [r["name"] for g in rules for r in g["rules"] if r.get("health") != "ok"] or "none")
    dash = httpx.get(f"{GRAFANA}/api/search", params={"query": "AI system"}, timeout=10).json()
    print("Grafana dashboards found:", [(d["title"], d["uid"]) for d in dash])
    sources = httpx.get(f"{GRAFANA}/api/datasources", auth=("admin", "lab-only-change-me"), timeout=10).json()
    print("Grafana data sources:", [(s["name"], s["type"]) for s in sources])

    run_dir = ROOT / "run-p3"
    shutil.rmtree(run_dir, ignore_errors=True)
    stack = Stack(run_dir=run_dir, gemini_key=key, pin_light=True, release="r1-baseline", version="phase3", otel_endpoint=OTLP, probe_interval_s=20)
    stack.start()
    try:
        print("services ready:", stack.wait_ready())
        ops = Ops(stack)

        banner("2. the baseline for the drift monitor: one probe rotation a person reviews, then saves")
        gw_key = (stack.secrets_dir / "gateway_key_monitor").read_text().strip()
        calibrate(stack.urls["identity"], stack.urls["gateway"], stack.urls["knowledge"], gw_key, save=True)
        stack.spawn("monitor")
        print("monitor ready:", stack.wait_ready(["monitor"]))

        banner("3. normal traffic: seven tickets")
        rows = [ops.ticket(f"N{i + 1}", t.text) for i, t in enumerate(NORMAL[:6])]
        pii = next(t for t in TICKETS if t.kind == "pii")
        rows.append(ops.ticket("N7", pii.text))
        print(tabulate([[r["id"], r["http"], r["action"], r["release"], f"{r['seconds']}s"] for r in rows], headers=["ticket", "HTTP", "action", "release", "time"], tablefmt="github"))
        time.sleep(12)                                              # let the exporters and the scrape catch up

        banner("4. one ticket is one trace across the services")
        for corr in ("p3-N1", "p3-N7"):
            traces = httpx.get(f"{JAEGER}/api/traces", params={"service": "tickets", "tags": json.dumps({"app.correlation_id": corr}), "limit": 5}, timeout=15).json()["data"]
            if not traces:
                print(corr, "-> no trace found")
                continue
            tr = max(traces, key=lambda t: len(t["spans"]))
            procs = {pid: p["serviceName"] for pid, p in tr["processes"].items()}
            by_service = {}
            for sp in tr["spans"]:
                by_service.setdefault(procs[sp["processID"]], []).append(sp["operationName"])
            print(f"{corr}: trace {tr['traceID']} has {len(tr['spans'])} spans across services {sorted(by_service)}")
            for svc in ("tickets", "identity", "gateway", "knowledge"):
                names = by_service.get(svc, [])
                print(f"   {svc:10s} {len(names):3d} spans, e.g. {sorted(set(names))[:4]}")
            tid = tr["traceID"]
            hits = {svc: sum(1 for ln in (run_dir / "logs" / f"{svc}.log").read_text(encoding="utf-8").splitlines() if tid in ln) for svc in ("tickets", "gateway", "knowledge", "identity")}
            print("   log lines carrying that trace id:", hits)

        banner("5. the metrics behind the SLOs (Prometheus)")
        queries = {
            "ticket outcomes by action": 'sum by (action) (ai_ticket_outcomes_total)',
            "gateway calls by route/tier": 'sum by (route, tier) (ai_gateway_requests_total)',
            "tokens by direction": 'sum by (direction) (ai_gateway_tokens_total)',
            "cost so far, USD": 'sum(ai_gateway_cost_usd_total)',
            "probe results": 'sum by (result) (ai_probe_results_total)',
            "drift verdict (0/1/2)": 'ai_drift_verdict',
            "probe correct ratio": 'ai_probe_correct_ratio',
            "release serving": 'ai_release_serving',
            "breaker state": 'max by (route) (ai_gateway_breaker_state)',
            "HTTP requests by service": 'sum by (service_name) (http_server_requests_total)',
        }
        rows = []
        for label, q in queries.items():
            res = prom(q)
            shown = "; ".join(f"{','.join(f'{k}={v}' for k, v in r['metric'].items() if k in ('action', 'route', 'tier', 'direction', 'result', 'release', 'role', 'service_name'))}={float(r['value'][1]):g}" for r in res)
            rows.append([label, len(res), shown[:150]])
        print(tabulate(rows, headers=["metric", "series", "values"], tablefmt="github"))
        print("SLO error ratios (5m):", {r["metric"]["__name__"].split(":")[2]: round(float(r["value"][1]), 3) for r in prom('{__name__=~"slo:error_ratio:.*:5m"}')})

        banner("6. the Grafana dashboard: every panel's query, run through Grafana's data source")
        board = json.loads((ROOT / "observability" / "grafana" / "dashboards" / "ai-system.json").read_text(encoding="utf-8"))
        rows = []
        for p in board["panels"]:
            counts = []
            for t in p["targets"]:
                r = httpx.get(f"{GRAFANA}/api/datasources/proxy/uid/prom/api/v1/query", params={"query": t["expr"]}, auth=("admin", "lab-only-change-me"), timeout=15).json()
                counts.append(len(r.get("data", {}).get("result", [])) if r.get("status") == "success" else f"ERR {r.get('error', '')[:30]}")
            rows.append([p["title"][:58], p["type"], counts])
        print(tabulate(rows, headers=["panel", "type", "series per query"], tablefmt="github"))

        def watch(seconds, until, label, every=10, tickets=None):
            """Poll the alert state until `until(firing_set)` or the time is up; optionally keep sending tickets."""
            start, seen, sent = time.time(), {}, 0
            while time.time() - start < seconds:
                if tickets and sent < tickets:
                    ops.ticket(f"{label}{sent + 1:02d}", NORMAL[sent % len(NORMAL)].text)
                    sent += 1
                else:
                    time.sleep(every)
                now = firing()
                for name in sorted(now - set(seen)):
                    seen[name] = round(time.time() - start)
                    print(stamp(), f"ALERT FIRING {name} after {seen[name]}s")
                if until(now):
                    break
            return seen, sent

        def clear(seconds, names, label, tickets=0):
            start, cleared, sent = time.time(), {}, 0
            while time.time() - start < seconds:
                if sent < tickets:
                    ops.ticket(f"{label}{sent + 1:02d}", NORMAL[sent % len(NORMAL)].text)
                    sent += 1
                else:
                    time.sleep(10)
                now = firing()
                for name in sorted(set(names) - now - set(cleared)):
                    cleared[name] = round(time.time() - start)
                    print(stamp(), f"ALERT CLEARED {name} after {cleared[name]}s")
                if set(names) <= set(cleared):
                    break
            return cleared

        banner("7. incident A, quality: a bad knowledge release is activated without the gate")
        print(stamp(), "knowledge: activate r3-helpful-answerer")
        ops.admin("knowledge", "/load", {"id": "r3-helpful-answerer"})
        ops.admin("knowledge", "/activate", {"id": "r3-helpful-answerer"})
        fired_a, _ = watch(420, lambda now: "DriftCritical" in now, "QA")
        mon = httpx.get(f"{stack.urls['monitor']}/state", headers=ops.h, timeout=10).json()
        print("monitor /state:", json.dumps({k: mon[k] for k in ("verdict", "correct_ratio", "leaks_total")}), "findings:", [f"{f['metric']}:{f['severity']}" for f in mon["findings"]])
        print("recent probes:", [(r["case"], "ok" if r["ok"] else "FAIL") for r in mon["recent"]])
        print(stamp(), "knowledge: rollback ->", ops.admin("knowledge", "/rollback", {"reason": "drift critical"})[1]["active"])
        cleared_a = clear(420, fired_a, "QR")
        print("restricted facts leaked by probes during the incident:", mon["leaks_total"])

        banner("8. incident B, dependency: the gateway process dies")
        print(stamp(), "stopping the gateway:", stack.stop_one("gateway"))
        fired_b, sent_b = watch(480, lambda now: "SLOBurn_ticket_safe_completion_ticket" in now, "OB", every=3, tickets=45)
        rows = [ops.ticket(f"OC{i}", NORMAL[i].text) for i in range(2)]
        print("tickets while the gateway is down:", [(r["http"], r["action"], f"{r['seconds']}s") for r in rows])
        print("signals at this moment:", {n: [round(float(r["value"][1]), 3) for r in prom(q)] for n, q in {
            "availability error ratio (5m)": "slo:error_ratio:ticket_availability:5m",
            "safe-completion error ratio (5m)": "slo:error_ratio:ticket_safe_completion:5m",
            "probe error ratio (5m)": "slo:error_ratio:answer_quality_probe:5m"}.items()})
        print("ticket service readiness:", stack.probe("tickets")[0], stack.probe("tickets")[1].get("checks"))
        print(stamp(), "restarting the gateway")
        stack.spawn("gateway")
        print("gateway ready:", stack.wait_ready(["gateway"]))
        cleared_b = clear(480, fired_b, "OR", tickets=8)
        time.sleep(25)
        delivered = httpx.get(f"{SINK}/alerts", timeout=10).json()

        banner("9. what the alert pipeline delivered, and the timeline")
        print("notifications received by the webhook sink:")
        print(tabulate([[a["alertname"], a["status"], a["severity"], (a["startsAt"] or "")[11:19]] for a in delivered], headers=["alert", "status", "severity", "starts (UTC)"], tablefmt="github"))
        print(tabulate([["A quality (bad knowledge release)", n, f"{fired_a[n]}s", f"{cleared_a.get(n, '-')}s"] for n in sorted(fired_a)] +
                       [["B dependency (gateway down)", n, f"{fired_b[n]}s", f"{cleared_b.get(n, '-')}s"] for n in sorted(fired_b)],
                       headers=["incident", "alert", "fired after", "cleared after the fix"], tablefmt="github"))
        print("still firing:", sorted(firing()) or "none")
    finally:
        print("\nstopping the services:", json.dumps(stack.stop()))
        subprocess.run(COMPOSE + ["down"], capture_output=True, text=True, env=env)


if __name__ == "__main__":
    main()
