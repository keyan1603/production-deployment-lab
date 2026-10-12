"""Capacity: where does the system stop getting faster, and what does scaling out actually buy?

    python -u scripts/phase4_capacity.py

SIMULATED PROVIDER. Every model call goes to the Gemini simulator (sim/gemini_sim.py), which enforces a requests-per-minute and a
requests-per-day quota like the real free tier. The services, the gateway, the Google SDK and the retry logic are the real ones; the
answers are scripted. The numbers describe how the system behaves under load against a rate-limited provider, not model quality.

    E1  concurrency sweep      closed-loop clients 1..16 against one ticket service
    E2  scale out the tickets  1, 2, 3 ticket-service instances, same offered load
    E3  scale out the gateway  one gateway versus two, each with its own rate limiter, one provider quota
    E4  the quota guard        a daily allowance that runs out mid-run, with and without the guard
"""

import json
import shutil
import sys
import time
import warnings
from pathlib import Path

import httpx
from tabulate import tabulate

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.loadgen import Sim, run_load  # noqa: E402
from deploylab.stack import ROOT, Stack  # noqa: E402
from tickets.data import TICKETS  # noqa: E402

TEXTS = [t.text for t in TICKETS if t.kind == "normal"]
HEAVY = "gemini-3.5-flash"
RPM = 60                      # the provider's per-minute quota, and the gateway's limiter for it


def banner(text):
    print(f"\n{'=' * 8} {text}")


def make_stack(name, sim, overrides=None, **kw):
    run = ROOT / "run-cap" / name
    shutil.rmtree(run, ignore_errors=True)
    stack = Stack(run_dir=run, gemini_key="sim-key-not-a-real-key", pin_light=True, rpm=kw.pop("rpm", RPM), port_base=kw.pop("port_base", 8300),
                  provider_url=sim.url, version=name, overrides=overrides, **kw)
    stack.start()
    stack.wait_ready()
    return stack


def token_factory(stack):
    return lambda: httpx.post(f"{stack.urls['identity']}/login", json={"username": "sam", "audience": "ticket-agent"}, timeout=10).json()["token"]


def row(label, run):
    s = run.summary()
    return [label, s["sent"], s["answered"], f"{s['tickets_per_min']}", s["p50_s"], s["p95_s"], s["max_s"], json.dumps(s["http"]), json.dumps(s["actions"])]


HEAD = ["run", "sent", "answered", "tickets/min", "p50 s", "p95 s", "max s", "HTTP codes", "outcomes"]


def main():
    sim = Sim(port=8900, rpm=RPM, rpd=100000)
    # The real free tier gives the heavy model a tiny allowance (20 a day). A simulated heavy model with the light model's limits would
    # quietly absorb every failover and make scaling look free.
    sim.config(rpd_by_model={HEAVY: 20}, rpm_by_model={HEAVY: 5})
    try:
        # ---------------------------------------------------------------- E1 and E2
        banner(f"E1. concurrency sweep: one ticket service, provider quota {RPM} requests/minute (about 15 tickets/minute at 4 calls each)")
        tickets_env = {"tickets": {"MAX_CONCURRENT_TICKETS": "16", "MAX_WAITING_TICKETS": "64"}}
        stack = make_stack("a", sim, tickets_env)
        tok = token_factory(stack)
        rows = []
        try:
            for workers, per in ((1, 10), (2, 8), (4, 6), (8, 4), (16, 3)):
                sim.reset()
                run = run_load([stack.urls["tickets"]], workers, per, tok, TEXTS, prefix=f"E1c{workers}-")
                st = sim.stats()
                rows.append(row(f"{workers} client(s)", run) + [st["max_inflight"], st["responses"].get("gemini-3.5-flash-lite:429", 0)])
                print(f"  {workers} clients done: {run.summary()['tickets_per_min']}/min")
            print(tabulate(rows, headers=HEAD + ["provider max in flight", "provider 429s"], tablefmt="github"))

            banner("E2. scale out the ticket service: 1, 2, 3 instances behind round-robin, 12 clients")
            rows = []
            urls = [stack.urls["tickets"]]
            for n in (1, 2, 3):
                while len(urls) < n:
                    key = f"x{len(urls)}"
                    stack.spawn_extra("tickets", key, 8350 + len(urls), {"MAX_CONCURRENT_TICKETS": "16", "MAX_WAITING_TICKETS": "64"})
                    stack.wait_ready([f"tickets-{key}"])
                    urls.append(stack.urls[f"tickets-{key}"])
                sim.reset()
                run = run_load(urls[:n], 12, 3, tok, TEXTS, prefix=f"E2n{n}-")
                rows.append(row(f"{n} instance(s)", run) + [sim.stats()["max_inflight"]])
                print(f"  {n} instances done: {run.summary()['tickets_per_min']}/min")
            print(tabulate(rows, headers=HEAD + ["provider max in flight"], tablefmt="github"))
        finally:
            stack.stop()

        # ---------------------------------------------------------------- E3
        sim.config(rpm=RPM, rpd=100000, rpd_by_model={HEAVY: 20}, rpm_by_model={HEAVY: 5})
        banner(f"E3. scale out the GATEWAY: each instance enforces its own {RPM}/minute limit against one {RPM}/minute provider quota")
        rows = []
        for label, two in (("one gateway", False), ("two gateways", True)):
            sim.reset()
            stack = make_stack("b", sim, tickets_env, port_base=8300)
            try:
                tok = token_factory(stack)
                urls = [stack.urls["tickets"]]
                if two:
                    stack.spawn_extra("gateway", "b", 8392)
                    stack.wait_ready(["gateway-b"])
                    stack.spawn_extra("tickets", "b", 8394, {"GATEWAY_URL": stack.urls["gateway-b"], "MAX_CONCURRENT_TICKETS": "16", "MAX_WAITING_TICKETS": "64"})
                    stack.wait_ready(["tickets-b"])
                    urls.append(stack.urls["tickets-b"])
                run = run_load(urls, 12, 3, tok, TEXTS, prefix="E3-")
                st = sim.stats()
                s = run.summary()
                rows.append(row(label, run) + [st["responses"].get("gemini-3.5-flash-lite:429", 0), st["responses"].get("gemini-3.5-flash-lite:200", 0)])
                print(f"  {label} done: {s['tickets_per_min']}/min, provider 429s {st['responses'].get('gemini-3.5-flash-lite:429', 0)}")
            finally:
                stack.stop()
        print(tabulate(rows, headers=HEAD + ["provider 429s", "provider 200s"], tablefmt="github"))

        # ---------------------------------------------------------------- E4
        banner("E4. the quota guard: a 140-request daily allowance runs out in the middle of a run")
        rows = []
        for label, guard in (("no guard", False), ("guard (tickets shed at 85%)", True)):
            sim.reset()
            sim.config(rpd=140, rpm=600, rpd_by_model={"gemini-3.5-flash": 20})
            over = {"gateway": {"REQUESTS_PER_MINUTE": "600"}, **tickets_env}
            if guard:
                over["gateway"].update({"QUOTA_RPD": "gemini-3.5-flash-lite=140", "QUOTA_SHED": "ticket-agent=0.85,knowledge-agent=0.98,monitor-agent=1.0"})
            stack = make_stack("c", sim, over, rpm=600)
            try:
                tok = token_factory(stack)
                t0 = time.time()
                run = run_load([stack.urls["tickets"]], 4, 12, tok, TEXTS, prefix="E4-", timeout=600)
                st = sim.stats()
                held = [r for r in run.results if r.action == "held"]
                released = [r for r in run.results if r.action == "released"]
                reasons: dict = {}
                for r in held:
                    key = (r.reasons[0].split(":")[0] if r.reasons else "?")
                    reasons[key] = reasons.get(key, 0) + 1
                avg = lambda xs: round(sum(r.seconds for r in xs) / len(xs), 1) if xs else None
                gw_key = (stack.secrets_dir / "gateway_key_monitor").read_text().strip()
                status = httpx.get(f"{stack.urls['gateway']}/v1/status", headers={"X-Service": "monitor-agent", "X-Service-Key": gw_key}, timeout=10).json()
                rows.append([label, len(released), len(held), json.dumps(reasons), avg(released), avg(held), st["responses"].get("gemini-3.5-flash-lite:429", 0),
                             st["requests_today"].get("gemini-3.5-flash-lite", 0), json.dumps((status.get("quota") or {}).get("gemini-3.5-flash-lite", {}).get("ratio"))])
                print(f"  {label} done in {time.time() - t0:.0f}s")
            finally:
                stack.stop()
        print(tabulate(rows, headers=["run", "released", "held", "hold reasons", "avg s released", "avg s held", "provider 429s", "requests that reached provider", "guard ratio"], tablefmt="github"))
        print("final provider stats:", json.dumps({k: v for k, v in sim.stats().items() if k in ("requests_today", "responses")}))
    finally:
        sim.stop()


if __name__ == "__main__":
    main()
