"""The same deployment checks as phase1_study.py, against the four containers (Podman + podman-compose).

    python scripts/vendor_system.py && podman build --format docker -f deploy/Dockerfile -t production-deployment-lab:phase1 .
    python scripts/make_secrets.py && podman-compose -f deploy/compose.yaml up -d
    python -u scripts/container_study.py

Checks what only a container runtime can show: the image's own health checks, an unprivileged read-only
filesystem, the platform's stop signal and grace period, and a container killed in the middle of a ticket.
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
import os  # noqa: E402
os.environ["SSL_CERT_FILE"] = str(Path(__file__).resolve().parent.parent / "secrets" / "ca.pem")   # trust the lab CA, as the services do
from scripts.phase1_study import REFUND, banner, login, submit  # noqa: E402


def podman(*args, check=True):
    p = subprocess.run(["podman", *args], capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


COMPOSE = ["podman-compose", "-f", "deploy/compose.yaml"]


def compose(*args):
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)


_FETCH = """
import sys, urllib.request, urllib.error
try:
    r = urllib.request.urlopen(sys.argv[1], timeout=3)
    print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
except Exception:
    print(0, "{}")
"""


class Containers:
    urls = {"identity": "https://localhost:8101", "tickets": "https://localhost:8104"}

    def probe(self, name, path="/readyz", timeout=4.0):
        # Only identity and tickets publish a port; the other two are probed from inside the network.
        if name in self.urls:
            try:
                r = httpx.get(f"{self.urls[name]}{path}", timeout=timeout)
                return r.status_code, (r.json() if r.content else {})
            except httpx.HTTPError:
                return 0, {}
        port = {"gateway": 8102, "knowledge": 8103}[name]
        code = podman("exec", "deploy_tickets_1", "python", "-c", _FETCH, f"https://{name}:{port}{path}", check=False)
        status, _, body = code.partition(" ")
        try:
            return int(status), json.loads(body)
        except ValueError:
            return 0, {}


def wait(pred, timeout=120.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return round(time.time() - t0, 1)
        time.sleep(0.5)
    return None


def main():
    c = Containers()
    stack = type("S", (), {"urls": {"identity": c.urls["identity"], "tickets": c.urls["tickets"]}})()

    banner("1. what the runtime reports")
    rows = []
    for name in ("identity", "gateway", "knowledge", "tickets"):
        info = json.loads(podman("inspect", f"deploy_{name}_1"))[0]
        health = info["State"].get("Health", {}).get("Status", "none")
        rows.append([name, info["State"]["Status"], health, info["Config"]["User"], info["HostConfig"]["ReadonlyRootfs"],
                     ",".join(info["HostConfig"].get("CapDrop") or []), info["Config"]["Image"].split("/")[-1]])
    print(tabulate(rows, headers=["container", "state", "health check", "user", "read-only fs", "cap_drop", "image"], tablefmt="github"))
    print("readiness:", {n: c.probe(n)[0] for n in ("identity", "gateway", "knowledge", "tickets")})

    banner("2. only the gateway container holds the model key")
    for name in ("identity", "gateway", "knowledge", "tickets"):
        env = json.loads(podman("inspect", f"deploy_{name}_1"))[0]["Config"]["Env"]
        mounts = podman("exec", f"deploy_{name}_1", "sh", "-c", "ls /run/secrets 2>/dev/null | tr '\\n' ' '")
        print(f"  {name:9s} GEMINI in environment: {any('GEMINI' in e for e in env)!s:5}  secret files: {mounts}")

    banner("3. a refund ticket through the containers, two operators")
    for operator in ("sam", "alice"):
        t = time.time()
        r = submit(stack, login(stack, operator), corr=f"c1-{operator}")
        out = r.json()
        print(f"[{operator}] HTTP {r.status_code} in {time.time() - t:.1f}s action={out['action']} model_calls={out['model_calls']} tools={sorted(set(out['tools_used']))}")
        print("   reply:", out["reply"][:300].replace("\n", " "))
    logs = podman("logs", "deploy_knowledge_1", check=False)
    print("knowledge container saw correlation ids:", sorted({json.loads(l)["correlation_id"] for l in logs.splitlines()
                                                              if l.startswith("{") and json.loads(l).get("correlation_id", "").startswith("c1-")}))

    banner("4. the read-only filesystem and the unprivileged user")
    print("write to the image filesystem:", podman("exec", "deploy_tickets_1", "sh", "-c", "touch /app/x 2>&1; echo exit=$?", check=False).replace("\n", " | "))
    print("whoami:", podman("exec", "deploy_tickets_1", "id", "-u"))

    banner("5. a hard dependency (gateway container) stops and returns")
    t0 = time.time()
    podman("stop", "-t", "70", "deploy_gateway_1")
    print(f"stopped in {time.time() - t0:.1f}s (exit code {podman('inspect', '-f', '{{.State.ExitCode}}', 'deploy_gateway_1')})")
    t_down = wait(lambda: c.probe("tickets")[0] == 503, 30)
    print(f"tickets reports unready after {t_down}s; liveness still: {c.probe('tickets', '/healthz')[0]}")
    podman("start", "deploy_gateway_1")
    t_up = wait(lambda: c.probe("tickets")[0] == 200, 90)
    print(f"tickets ready again {t_up}s after the gateway container restarted (nothing else was restarted)")

    banner("6. the platform's stop signal during a ticket vs a kill")
    for mode in ("podman stop (SIGTERM, grace period)", "podman kill (SIGKILL)"):
        token = login(stack, "sam")
        outcome = {}

        def run():
            t = time.time()
            try:
                r = submit(stack, token, corr="c1-drill", tid="P03", timeout=240)
                outcome.update(status=r.status_code, action=r.json().get("action"), seconds=round(time.time() - t, 1))
            except httpx.HTTPError as exc:
                outcome.update(status=f"connection lost ({type(exc).__name__})", seconds=round(time.time() - t, 1))

        th = threading.Thread(target=run)
        th.start()
        time.sleep(4)
        seen = []
        if mode.startswith("podman stop"):
            def watch():
                for _ in range(60):
                    code, body = c.probe("tickets", timeout=1.5)
                    seen.append((code, body.get("status")))
                    if code == 0:
                        break
                    time.sleep(0.4)
            w = threading.Thread(target=watch)
            w.start()
            t0 = time.time()
            podman("stop", "-t", "70", "deploy_tickets_1")
            w.join()
        else:
            t0 = time.time()
            podman("kill", "deploy_tickets_1")
        took = round(time.time() - t0, 1)
        th.join(timeout=240)
        code = podman("inspect", "-f", "{{.State.ExitCode}}", "deploy_tickets_1")
        print(f"\n[{mode}] container stopped after {took}s, exit code {code}")
        print(f"[{mode}] the in-flight ticket: {outcome}")
        if seen:
            dedup = [s for i, s in enumerate(seen) if i == 0 or s != seen[i - 1]]
            print("   readiness while it shut down:", dedup)
        podman("start", "deploy_tickets_1")
        wait(lambda: c.probe("tickets")[0] == 200, 90)

    banner("7. bad configuration inside a container")
    p = subprocess.run(["podman", "run", "--rm", "localhost/production-deployment-lab:phase1", "python", "-m", "services.gateway_service"],
                       capture_output=True, text=True)
    print("exit code:", p.returncode)
    for line in p.stdout.splitlines():
        if line.startswith("{"):
            print("  ", json.loads(line)["msg"].replace("\n", " | "))


if __name__ == "__main__":
    main()
