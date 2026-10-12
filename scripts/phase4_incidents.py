"""Incident drills: break the system on purpose and measure what the signals do.

    python -u scripts/phase4_incidents.py

SIMULATED PROVIDER. The provider simulator (sim/gemini_sim.py) stands in for the model API, so a failure can be switched on and off on
demand and nothing spends a real quota. Everything else is the real thing: the four services and the monitor on the host, the real Google
SDK, the real gateway with its breakers and guards, and the open-source observability stack in Podman with the generated SLO rules at
minute-scale windows. Background ticket traffic runs through every drill, so each one reports its customer impact as well as its alerts.

    D1  provider 429 storm      every model refuses; the gateway fails over to the local model
    D2  slow light model        the light model answers in 40 s; the gateway times out and fails over
    D3  a pin sends all traffic to the expensive tier (a configuration change the release gate does not see)
    D4  the daily allowance runs out, with the quota guard on
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import httpx
from tabulate import tabulate

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.loadgen import Sim  # noqa: E402
from deploylab.stack import ROOT, Stack  # noqa: E402
from scripts.save_baseline import calibrate  # noqa: E402
from tickets.data import TICKETS  # noqa: E402

PROM, ALERTM, SINK = "http://127.0.0.1:9090", "http://127.0.0.1:9093", "http://127.0.0.1:9099"
OTLP = "http://127.0.0.1:4318"
COMPOSE = [os.environ.get("PODMAN_COMPOSE", "podman-compose"), "-f", "observability/compose.yaml"]
TEXTS = [t.text for t in TICKETS if t.kind == "normal"]
T0 = time.time()
LIGHT = "gemini-3.5-flash-lite"


def banner(text):
    print(f"\n{'=' * 8} {text}")


def stamp():
    return f"+{time.time() - T0:5.0f}s"


def firing():
    alerts = httpx.get(f"{PROM}/api/v1/alerts", timeout=10).json()["data"]["alerts"]
    return {a["labels"]["alertname"] for a in alerts if a["state"] == "firing"}


def prom(query):
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": query}, timeout=15).json()
    return r["data"]["result"] if r.get("status") == "success" else []


class Traffic:
    """Closed-loop background tickets through the whole drill, with a timestamp on every result."""

    def __init__(self, url_for, token_for, workers=2):
        self.url_for, self.token_for, self.workers = url_for, token_for, workers
        self.results, self.stop_flag, self.lock, self.n = [], threading.Event(), threading.Lock(), 0
        self.threads = [threading.Thread(target=self.loop, daemon=True) for _ in range(workers)]

    def loop(self):
        while not self.stop_flag.is_set():
            with self.lock:
                self.n += 1
                n = self.n
            tid, t0 = f"BG{n:05d}", time.time()
            try:
                r = httpx.post(f"{self.url_for()}/tickets", json={"id": tid, "text": TEXTS[n % len(TEXTS)]}, timeout=400,
                               headers={"Authorization": f"Bearer {self.token_for()}", "X-Correlation-Id": f"bg-{tid}"})
                body = r.json() if r.status_code == 200 else {}
                rec = (t0, time.time(), r.status_code, body.get("action", ""), (body.get("reasons") or [""])[0].split(":")[0])
            except httpx.HTTPError as exc:
                rec = (t0, time.time(), 0, "", type(exc).__name__)
            with self.lock:
                self.results.append(rec)
            self.stop_flag.wait(0.5)

    def start(self):
        [t.start() for t in self.threads]

    def stop(self):
        self.stop_flag.set()

    def window(self, a, b):
        with self.lock:
            rows = [r for r in self.results if a <= r[1] <= b]
        done = [r for r in rows if r[2] == 200]
        lat = sorted(r[1] - r[0] for r in done)
        reasons: dict = {}
        for r in done:
            if r[3] == "held":
                reasons[r[4]] = reasons.get(r[4], 0) + 1
        return {"tickets": len(rows), "released": sum(1 for r in done if r[3] == "released"), "held": sum(1 for r in done if r[3] == "held"),
                "errors": len(rows) - len(done), "p95_s": round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else None, "hold_reasons": reasons}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drills", default="D1,D2,D3,D4")
    chosen = set(ap.parse_args().drills.split(","))
    env = {**os.environ, "SLO_RULES": "slo.lab.rules.yml"}
    run_dir = ROOT / "run-inc"
    shutil.rmtree(run_dir, ignore_errors=True)
    (run_dir / "state").mkdir(parents=True)
    subprocess.run(COMPOSE + ["down"], capture_output=True, text=True, env=env)
    up = subprocess.run(COMPOSE + ["up", "-d"], capture_output=True, text=True, env=env)
    if up.returncode:
        raise SystemExit(up.stderr[-500:])
    sim = Sim(port=8910, rpm=600, rpd=100000)
    guard = {"QUOTA_RPD": f"{LIGHT}=5000", "QUOTA_SHED": "ticket-agent=0.85,knowledge-agent=0.98,monitor-agent=1.0", "STATE_DIR": str(run_dir / "state"),
             "REQUESTS_PER_MINUTE": "600"}
    overrides = {"gateway": dict(guard), "tickets": {"MAX_CONCURRENT_TICKETS": "16", "MAX_WAITING_TICKETS": "64"},
                 "monitor": {"BASELINE_PATH": str(run_dir / "baseline.json")}}
    stack = Stack(run_dir=run_dir, gemini_key="sim-key-not-a-real-key", pin_light=True, rpm=600, port_base=8500, provider_url=sim.url, version="drills",
                  otel_endpoint=OTLP, probe_interval_s=10, overrides=overrides)
    traffic = None
    try:
        for _ in range(90):
            try:
                if httpx.get(f"{PROM}/-/ready", timeout=2).status_code == 200 and httpx.get(f"{SINK}/alerts", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(1)
        stack.start()
        print("services ready:", stack.wait_ready())
        gw_key = (stack.secrets_dir / "gateway_key_monitor").read_text().strip()
        calibrate(stack.urls["identity"], stack.urls["gateway"], stack.urls["knowledge"], gw_key, path=run_dir / "baseline.json", save=True, log=lambda *a: None)
        stack.spawn("monitor")
        stack.wait_ready(["monitor"])
        token = lambda: httpx.post(f"{stack.urls['identity']}/login", json={"username": "sam", "audience": "ticket-agent"}, timeout=10).json()["token"]
        traffic = Traffic(lambda: stack.urls["tickets"], token)
        traffic.start()
        banner("steady state: two clients, six probes in rotation every 10 s, nothing should fire")
        time.sleep(150)
        steady = traffic.window(time.time() - 120, time.time())
        print(stamp(), "customer view over the last two minutes:", json.dumps(steady))
        print(stamp(), "alerts firing:", sorted(firing()) or "none")
        cost = prom("sum(rate(ai_gateway_cost_usd_total[2m])) / sum(rate(ai_ticket_outcomes_total[2m]))")
        print(stamp(), "cost per ticket right now (simulated tokens, published prices): $%.5f" % float(cost[0]["value"][1]) if cost else "cost: no data")

        summary = []

        def drill(name, inject, fix, expect, max_wait=420, clear_wait=420, note=""):
            banner(name)
            t_inject = time.time()
            print(stamp(), "INJECT:", note)
            inject()
            fired = {}
            while time.time() - t_inject < max_wait:
                time.sleep(5)
                for alert in sorted(firing() - set(fired)):
                    fired[alert] = round(time.time() - t_inject)
                    print(stamp(), f"ALERT FIRING {alert} after {fired[alert]}s")
                if set(expect) <= set(fired):
                    break
            during = traffic.window(t_inject, time.time())
            print(stamp(), "customer impact so far:", json.dumps(during))
            t_fix = time.time()
            print(stamp(), "FIX")
            fix()
            cleared = {}
            while time.time() - t_fix < clear_wait:
                time.sleep(5)
                now = firing()
                for alert in sorted(set(fired) - now - set(cleared)):
                    cleared[alert] = round(time.time() - t_fix)
                    print(stamp(), f"ALERT CLEARED {alert} after {cleared[alert]}s")
                if set(fired) <= set(cleared):
                    break
            after = traffic.window(t_fix + 15, time.time())
            print(stamp(), "customer view after the fix:", json.dumps(after))
            summary.append((name.split(":")[0], fired, cleared, during, after))

        # D1 ------------------------------------------------------------------------------------
        if "D1" in chosen:
          drill("D1: provider 429 storm", lambda: sim.faults("error_429", model="*"), lambda: sim.faults("none"),
              expect=["CircuitBreakerOpen", "DegradedTierServing"], note="the simulated provider answers 429 RESOURCE_EXHAUSTED to every model")
        # D2 ------------------------------------------------------------------------------------
        if "D2" in chosen:
          drill("D2: slow light model", lambda: sim.faults("slow", model=LIGHT, latency_ms=40000), lambda: sim.faults("none"),
              expect=["CircuitBreakerOpen"], note="the light model takes 40 s longer per call (the gateway times it out at 30 s)")

        # D3 ------------------------------------------------------------------------------------
        def restart_gateway(reset_state=False, **env):
            """Stop the gateway FIRST, then reset any state: a gateway that is still shutting down writes its counters back."""
            print(stamp(), "restarting the gateway with", env or "the standard configuration")
            stack.stop_one("gateway")
            if reset_state:
                shutil.rmtree(run_dir / "state", ignore_errors=True)
                (run_dir / "state").mkdir()
            stack.overrides["gateway"] = {**guard, **env}
            stack.spawn("gateway")
            stack.wait_ready(["gateway"])

        if "D3" in chosen:
          drill("D3: a pin sends all traffic to the heavy tier", lambda: restart_gateway(FORCE_TIER="heavy"), lambda: restart_gateway(),
              expect=["CostPerTicketHigh"], note="FORCE_TIER=heavy on the gateway: a configuration change, so no release gate ran")

        # D4 ------------------------------------------------------------------------------------
        def small_allowance():
            sim.reset()
            sim.config(rpd=300)
            guard["QUOTA_RPD"] = f"{LIGHT}=300"
            restart_gateway(reset_state=True)

        def next_day():
            sim.reset()
            sim.config(rpd=100000)
            guard["QUOTA_RPD"] = f"{LIGHT}=5000"
            restart_gateway(reset_state=True)

        if "D4" in chosen:
          drill("D4: the daily allowance runs out (quota guard on)", small_allowance, next_day,
              expect=["QuotaNearlyExhausted", "QuotaGuardShedding"], max_wait=540, note="a 300-request allowance; tickets are shed at 85 percent, probes keep going")

        traffic.stop()
        time.sleep(20)
        banner("what Alertmanager delivered to the webhook sink")
        delivered = httpx.get(f"{SINK}/alerts", timeout=10).json()
        print(tabulate([[a["alertname"], a["status"], a["severity"], (a["startsAt"] or "")[11:19]] for a in delivered], headers=["alert", "status", "severity", "starts (UTC)"], tablefmt="github"))

        banner("summary")
        rows = []
        for name, fired, cleared, during, after in summary:
            rows.append([name, "; ".join(f"{a} {t}s" for a, t in fired.items()) or "none", "; ".join(f"{a} {t}s" for a, t in cleared.items()) or "-",
                         f"{during['tickets']} tickets: {during['released']} released, {during['held']} held, {during['errors']} errors, p95 {during['p95_s']}s",
                         f"{after['released']}/{after['tickets']} released"])
        print(tabulate(rows, headers=["drill", "alerts fired after", "cleared after the fix", "customer impact during", "after the fix"], tablefmt="github"))
        print("provider saw:", json.dumps({k: v for k, v in sim.stats().items() if k in ("responses", "max_inflight")}))
    finally:
        if traffic:
            traffic.stop()
        print("\nstopping:", json.dumps(stack.stop()))
        sim.stop()
        subprocess.run(COMPOSE + ["down"], capture_output=True, text=True, env=env)


if __name__ == "__main__":
    main()
