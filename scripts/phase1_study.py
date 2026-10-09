"""Services as real processes: a live run on real models.

    python -u scripts/phase1_study.py

Starts identity, gateway, knowledge and tickets as four OS processes and measures what a deployment needs
to be true: they find each other without a start order; only the gateway holds the model key; a ticket flows
through every service with one correlation id; bad configuration refuses to start with every problem listed;
a dependency outage makes the right services unready (and only those); and a stop signal in the middle of a
ticket lets the ticket finish while a hard kill loses it.
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.stack import MODULES, Stack, base_env, load_gemini_key  # noqa: E402

REFUND = ("Customer C-1002 reports order O-5002 was charged twice. Can we refund the duplicate, how long will it take, "
          "and what is the approval threshold for refunds?")


def banner(text):
    print(f"\n{'=' * 8} {text}")


def login(stack, user, aud="ticket-agent"):
    r = httpx.post(f"{stack.urls['identity']}/login", json={"username": user, "audience": aud}, timeout=5)
    return r.json()["token"]


def submit(stack, token, text=REFUND, corr=None, timeout=240, tid="P01"):
    headers = {"Authorization": f"Bearer {token}"}
    if corr:
        headers["X-Correlation-Id"] = corr
    return httpx.post(f"{stack.urls['tickets']}/tickets", json={"id": tid, "text": text}, headers=headers, timeout=timeout)


def main():
    key = load_gemini_key()
    assert key, "no GEMINI_API_KEY found"
    stack = Stack(run_dir=Path("run"), gemini_key=key, pin_light=True, version="phase1")
    try:
        run(stack)
    finally:
        stack.stop()                                   # never leave four orphan processes behind


def run(stack):

    # ---- 1. start all four at once -------------------------------------------------------------
    banner("1. start four processes at once, no start order")
    t0 = time.time()
    stack.start()
    ready = stack.wait_ready()
    print(tabulate([[n, f"{s}s", stack.procs[n].popen.pid] for n, s in sorted(ready.items(), key=lambda kv: kv[1])],
                   headers=["service", "ready after", "pid"], tablefmt="github"))
    code, body = stack.probe("tickets")
    print("tickets /readyz:", json.dumps(body))
    code, body = stack.probe("knowledge")
    print("knowledge /readyz:", json.dumps(body))

    # ---- 2. least privilege --------------------------------------------------------------------
    banner("2. what each process was given (secret values are never passed; files are)")
    rows = []
    for n in MODULES:
        env = stack.environment_of(n)
        secretish = sorted(k for k in env if k.endswith("_FILE"))
        rows.append([n, ", ".join(secretish), "yes" if "GEMINI_API_KEY_FILE" in env else "no"])
    print(tabulate(rows, headers=["service", "secret files mounted", "model API key"], tablefmt="github"))
    leaked = [n for n in MODULES if n != "gateway" and "GEMINI_API_KEY" in "".join(stack.environment_of(n))]
    print("services other than the gateway that can see the model key:", leaked or "none")

    # ---- 3. a ticket through every service -----------------------------------------------------
    banner("3. one refund ticket, two operators, four services")
    results = {}
    for operator in ("sam", "alice"):
        corr = f"p1-{operator}"
        t = time.time()
        r = submit(stack, login(stack, operator), corr=corr)
        out = r.json()
        results[operator] = (out, corr)
        reply = out["reply"].lower()
        print(f"\n[{operator}] HTTP {r.status_code} in {time.time() - t:.1f}s action={out['action']} model_calls={out['model_calls']} "
              f"tools={sorted(set(out['tools_used']))} correlation={r.headers.get('x-correlation-id')}")
        print("   reply:", out["reply"][:360].replace("\n", " "))
        print("   states the support-only threshold:", "500 dollars" in reply or "team lead" in reply)

    banner("3b. what the shared gateway did with each operator's four model calls")
    rows = []
    for line in (stack.run / "audit" / "gateway.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["correlation_id"] in ("p1-sam", "p1-alice"):
            rows.append([rec["correlation_id"], rec["tenant"], rec["outcome"], rec["route"], rec["input_tokens"], rec["output_tokens"]])
    print(tabulate(rows, headers=["correlation", "cache scope (tenant)", "outcome", "route", "in", "out"], tablefmt="github"))
    print("served from the cache for the ticket service:", sum(1 for r in rows if r[2].startswith("cache")))
    print("\nknowledge agent audit (who the policy lookup was answered for):")
    for line in (stack.run / "audit" / "knowledge_agent.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        print(f"   {rec['correlation_id']}: {rec['outcome']} groups={rec['effective_groups']} cited={rec['cited']}")

    # ---- 4. one correlation id across the services' logs ---------------------------------------
    banner("4. the correlation id p1-sam in every service's log")
    rows = []
    for n in MODULES:
        lines = [json.loads(l) for l in stack.procs[n].log_path.read_text(encoding="utf-8").splitlines() if l.startswith("{")]
        mine = [l for l in lines if l.get("correlation_id") == "p1-sam"]
        rows.append([n, len(mine), ", ".join(sorted({l.get("path", l["msg"])[:28] for l in mine}))[:70]])
    print(tabulate(rows, headers=["service", "log lines", "paths / events"], tablefmt="github"))

    # ---- 5. attacks over HTTP ------------------------------------------------------------------
    banner("5. the front door refuses bad credentials")
    from identity.tokens import issue
    forged = issue(b"attacker" * 6, "erin", ["exec"], "ticket-agent")
    wrong_aud = login(stack, "sam", "knowledge-agent")
    attacks = [("no token", {}), ("forged signature", {"Authorization": f"Bearer {forged}"}),
               ("token for another audience", {"Authorization": f"Bearer {wrong_aud}"})]
    rows = []
    for name, headers in attacks:
        r = httpx.post(f"{stack.urls['tickets']}/tickets", json={"text": "hi"}, headers=headers, timeout=10)
        rows.append([name, r.status_code, r.json().get("reason")])
    print(tabulate(rows, headers=["attack", "HTTP", "reason"], tablefmt="github"))

    # ---- 6. configuration that refuses to start ------------------------------------------------
    banner("6. a gateway started with no configuration")
    env = base_env()
    p = subprocess.run([sys.executable, "-m", MODULES["gateway"]], env=env, capture_output=True, text=True, timeout=60)
    print("exit code:", p.returncode)
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            print("  ", json.loads(line)["msg"].replace("\n", " | "))

    # ---- 7. dependency outages -----------------------------------------------------------------
    banner("7. a soft dependency (knowledge) and a hard dependency (gateway) go down, then come back")

    def until(fn, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if fn():
                return round(time.time() - t0, 1)
            time.sleep(0.25)
        return None

    def checks_of(name):
        return stack.probe(name, timeout=3)[1].get("checks", {})

    print("stop knowledge:", stack.stop_one("knowledge"))
    seen = until(lambda: checks_of("tickets").get("soft:knowledge", "ok") != "ok")
    code, _ = stack.probe("tickets")
    print(f"  tickets noticed knowledge is gone after {seen}s; tickets /readyz is still {code} (soft dependency: reported, not gating)")
    r = submit(stack, login(stack, "sam"), corr="p1-no-knowledge", tid="P02")
    o = r.json()
    print(f"  ticket while knowledge is down: HTTP {r.status_code} action={o['action']} reasons={o['reasons']}")
    stack.spawn("knowledge")
    print("  restart knowledge ->", stack.wait_ready(["knowledge"]))
    print("  tickets sees it again after", until(lambda: checks_of("tickets").get("soft:knowledge") == "ok"), "s")

    print("stop gateway:", stack.stop_one("gateway"))
    t_down = {n: until(lambda n=n: stack.probe(n, timeout=3)[0] == 503) for n in ("knowledge", "tickets")}
    print("  seconds until each service reports unready:", t_down)
    probe_ms = []
    for _ in range(5):
        t0 = time.perf_counter()
        stack.probe("tickets")
        probe_ms.append((time.perf_counter() - t0) * 1000)
    print(f"  /readyz latency while a dependency is down (5 probes): {', '.join(f'{m:.0f} ms' for m in probe_ms)}")
    rows = []
    for n in ("tickets", "knowledge", "identity"):
        code, body = stack.probe(n)
        _, health = stack.probe(n, "/healthz")
        rows.append([n, f"readyz {code}", f"healthz {'200' if health.get('status') == 'ok' else 'down'}",
                     "; ".join(f"{k}={v}" for k, v in body.get("checks", {}).items() if v != "ok")[:70]])
    print(tabulate(rows, headers=["service", "ready?", "alive?", "failing checks"], tablefmt="github"))
    stack.spawn("gateway")
    print("restart gateway ->", stack.wait_ready(["gateway"]))
    t_up = {n: until(lambda n=n: stack.probe(n, timeout=3)[0] == 200) for n in ("knowledge", "tickets")}
    print("  seconds until each service is ready again, with no restart of its own:", t_up)
    print("  after recovery:", {n: stack.probe(n)[0] for n in ("tickets", "knowledge", "gateway", "identity")})

    # ---- 8. stop signal during a ticket vs a hard kill -----------------------------------------
    banner("8. stop signal during a ticket: finish the work; hard kill: lose it")
    for mode in ("graceful", "hard kill"):
        token = login(stack, "sam")
        outcome = {}

        def run():
            t = time.time()
            try:
                r = submit(stack, token, corr=f"p1-{mode.replace(' ', '-')}", tid="P03", timeout=240)
                outcome.update(status=r.status_code, action=r.json().get("action"), seconds=round(time.time() - t, 1))
            except httpx.HTTPError as exc:
                outcome.update(status=f"connection lost ({type(exc).__name__})", seconds=round(time.time() - t, 1))

        th = threading.Thread(target=run)
        th.start()
        time.sleep(4)                                              # the ticket is now in flight
        _, before = stack.probe("tickets")
        observed = []

        def watch():
            for _ in range(40):
                code, body = stack.probe("tickets", timeout=1.5)
                refuse = None
                try:
                    refuse = httpx.post(f"{stack.urls['tickets']}/tickets", json={"text": "late"}, timeout=3,
                                        headers={"Authorization": f"Bearer {token}"}).status_code
                except httpx.HTTPError:
                    refuse = "refused connection"
                observed.append((code, body.get("status"), refuse))
                if code == 0:
                    break
                time.sleep(0.4)

        w = threading.Thread(target=watch) if mode == "graceful" else None
        if w:
            w.start()
        t_stop = time.time()
        if mode == "graceful":
            exit_info = stack.stop_one("tickets")
        else:
            stack.procs["tickets"].popen.kill()
            stack.procs["tickets"].popen.wait()
            exit_info = {"exit": stack.procs["tickets"].popen.returncode, "seconds": round(time.time() - t_stop, 1)}
        th.join(timeout=240)
        if w:
            w.join()
        print(f"\n[{mode}] in flight at signal: {before.get('inflight')}; process: {exit_info}")
        print(f"[{mode}] the in-flight ticket: {outcome}")
        if observed:
            print("[graceful] during shutdown, (readyz code, status, new ticket answer):")
            seen = []
            for o in observed:
                if not seen or seen[-1] != o:
                    seen.append(o)
            for o in seen:
                print("   ", o)
        stack.spawn("tickets")
        stack.wait_ready(["tickets"])

    banner("stopping the stack")
    print(json.dumps(stack.stop()))


if __name__ == "__main__":
    main()
