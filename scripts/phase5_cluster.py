"""The system on Kubernetes (kind on Podman): posture, network policy, a rolling update under load, self-healing and a node drain.

    python -u scripts/phase5_cluster.py [--keep]

SIMULATED PROVIDER. The model API is the provider simulator running as a pod (sim/gemini_sim.py): this cluster holds no model API key. The
services are the real ones, in a real Kubernetes cluster (three nodes, kindnet network policy, Pod Security "restricted" enforced by the
API server). What is measured is how the platform treats the system; nothing here says anything about answer quality.
"""

import json
import os
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import httpx
from tabulate import tabulate

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SSL_CERT_FILE"] = str(ROOT / "secrets-k8s" / "ca.pem")          # the lab CA, as the services trust it
from tickets.data import TICKETS  # noqa: E402

CTX, NS = "kind-ai-lab", "ai-lab"
TEXTS = [t.text for t in TICKETS if t.kind == "normal"]
TICKETS_URL, IDENTITY_URL = "https://localhost:30104", "https://localhost:30101"
REFUND = ("Customer C-1002 reports order O-5002 was charged twice. Can we refund the duplicate, and what is the approval threshold for refunds?")


def banner(text):
    print(f"\n{'=' * 8} {text}")


def k(*args, check=False, timeout=240, input=None):
    p = subprocess.run(["kubectl", "--context", CTX, "-n", NS, *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip()[:300])
    return (p.stdout + p.stderr).strip()


def pods(name):
    out = k("get", "pods", "-l", f"app.kubernetes.io/name={name}", "-o", "jsonpath={range .items[*]}{.metadata.name} {.spec.nodeName} {.status.phase}{\"\\n\"}{end}")
    return [line.split() for line in out.splitlines() if line.strip()]


def token():
    return httpx.post(f"{IDENTITY_URL}/login", json={"username": "sam", "audience": "ticket-agent"}, timeout=10).json()["token"]


class Load:
    """Closed-loop background tickets with a timestamp on every result."""

    def __init__(self, workers=4):
        self.results, self.stop_flag, self.lock, self.n = [], threading.Event(), threading.Lock(), 0
        self.threads = [threading.Thread(target=self.loop, daemon=True) for _ in range(workers)]

    def loop(self):
        while not self.stop_flag.is_set():
            with self.lock:
                self.n += 1
                n = self.n
            t0 = time.time()
            try:
                r = httpx.post(f"{TICKETS_URL}/tickets", json={"id": f"K{n:05d}", "text": TEXTS[n % len(TEXTS)]}, timeout=120,
                               headers={"Authorization": f"Bearer {token()}", "X-Correlation-Id": f"k8s-{n}"})
                body = r.json() if r.status_code == 200 else {}
                rec = (t0, time.time(), r.status_code, body.get("action", ""),
                       (body.get("reasons") or [""])[0].split(":")[0] if r.status_code == 200 else f"{r.headers.get('x-service', '-')}|{r.headers.get('retry-after', '-')}|{r.text[:70]}")
            except httpx.HTTPError as exc:
                rec = (t0, time.time(), 0, "", type(exc).__name__)
            with self.lock:
                self.results.append(rec)
            self.stop_flag.wait(0.3)

    def start(self):
        [t.start() for t in self.threads]
        return self

    def stop(self):
        self.stop_flag.set()
        [t.join(timeout=150) for t in self.threads]

    def window(self, a=0, b=1e18):
        with self.lock:
            rows = [r for r in self.results if a <= r[1] <= b]
        ok = [r for r in rows if r[2] == 200]
        lat = sorted(r[1] - r[0] for r in ok)
        failures: dict = {}
        for r in rows:
            if r[2] != 200:
                key = f"HTTP {r[2]}" if r[2] else r[4]
                failures[key] = failures.get(key, 0) + 1
        return {"tickets": len(rows), "released": sum(1 for r in ok if r[3] == "released"), "held": sum(1 for r in ok if r[3] == "held"),
                "failed": len(rows) - len(ok), "how": failures, "p95_s": round(lat[int(0.95 * (len(lat) - 1))], 1) if lat else None}


def under_load(label, action, seconds_before=25, seconds_after=30):
    load = Load().start()
    time.sleep(seconds_before)
    t0 = time.time()
    detail = action()
    t1 = time.time()
    time.sleep(seconds_after)
    load.stop()
    w = load.window(t0 - 5, time.time())
    print(f"[{label}] action took {t1 - t0:.0f}s; tickets finishing in the window: {json.dumps(w)}")
    return t1 - t0, w, detail


def main():
    keep = "--keep" in sys.argv
    banner("1. the cluster")
    print(k("get", "nodes", "-o", "wide").replace("\n", "\n  ")[:600])
    print(tabulate([[n, " ".join(f"{p[0]}@{p[1].replace('ai-lab-', '')}" for p in pods(n))] for n in ("identity", "gateway", "knowledge", "tickets", "provider-sim")],
                   headers=["deployment", "pods (name@node)"], tablefmt="github"))
    print("rollout settings: gateway strategy =", k("get", "deploy", "gateway", "-o", "jsonpath={.spec.strategy.type}"), "| tickets maxUnavailable =",
          k("get", "deploy", "tickets", "-o", "jsonpath={.spec.strategy.rollingUpdate.maxUnavailable}"), "| PDB:", k("get", "pdb", "tickets", "-o", "jsonpath={.spec.minAvailable}"))

    banner("2. tickets through the cluster, from outside, over TLS")
    for tid, text in (("C1", REFUND), ("C2", TEXTS[1])):
        t = time.time()
        r = httpx.post(f"{TICKETS_URL}/tickets", json={"id": tid, "text": text}, timeout=120, headers={"Authorization": f"Bearer {token()}", "X-Correlation-Id": f"k8s-first-{tid}"})
        b = r.json()
        print(f"  {tid}: HTTP {r.status_code} {b['action']} model_calls={b['model_calls']} tools={sorted(set(b['tools_used']))} in {time.time() - t:.1f}s")
    for svc in ("tickets", "gateway", "knowledge", "identity"):
        lines = k("logs", "-l", f"app.kubernetes.io/name={svc}", "--tail=200", "--prefix=false")
        print(f"  pods of {svc}: {sum(1 for ln in lines.splitlines() if 'k8s-first-C1' in ln)} log lines carry the correlation id of ticket C1")

    banner("3. posture the platform enforces")
    rows = []
    for svc in ("identity", "gateway", "knowledge", "tickets"):
        pod = pods(svc)[0][0]
        uid = k("exec", pod, "--", "id", "-u")
        write = k("exec", pod, "--", "python", "-c", "open('/app/x','w')")
        files = k("exec", pod, "--", "sh", "-c", "ls /run/secrets | tr '\\n' ' '")
        rows.append([svc, uid, "write refused" if "Read-only" in write else write[-40:], files])
    print(tabulate(rows, headers=["pod", "uid", "write to the image", "secret files visible"], tablefmt="github"))
    privileged = ("apiVersion: v1\nkind: Pod\nmetadata: {name: bad}\nspec:\n  containers:\n  - name: c\n    image: busybox\n    securityContext: {privileged: true}\n")
    print("applying a privileged pod:", k("apply", "-f", "-", input=privileged)[:260].replace("\n", " "))

    banner("4. network policy (default deny; each service admits only its callers)")

    def probe(name, host, port, labels=""):
        overrides = json.dumps({"spec": {"securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                                         "containers": [{"name": name, "image": "localhost/production-deployment-lab:phase5", "imagePullPolicy": "Never",
                                                         "command": ["python", "-c", f"import socket,sys\ntry:\n socket.create_connection(('{host}',{port}),timeout=5);print('CONNECTED')\nexcept Exception as e:\n print('BLOCKED',type(e).__name__)"],
                                                         "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True,
                                                                             "seccompProfile": {"type": "RuntimeDefault"}}}]}})
        args = ["run", name, "--rm", "-i", "--restart=Never", "--image=localhost/production-deployment-lab:phase5", "--overrides", overrides, "--quiet"]
        if labels:
            args += ["--labels", labels]
        for attempt in range(2):                      # a pod that finishes before kubectl attaches prints nothing: try again
            out = k(*args, timeout=90)
            if "CONNECTED" in out or "BLOCKED" in out:
                break
            time.sleep(2)
        return "CONNECTED" if "CONNECTED" in out else ("BLOCKED" if "BLOCKED" in out else out[-80:])

    rows = [["an unlabelled pod", "tickets:8104 (front door)", probe("np-a", "tickets", 8104)],
            ["an unlabelled pod", "gateway:8102", probe("np-b", "gateway", 8102)],
            ["an unlabelled pod", "knowledge:8103", probe("np-c", "knowledge", 8103)],
            ["a pod labelled as tickets", "gateway:8102", probe("np-d", "gateway", 8102, "app.kubernetes.io/name=tickets")]]
    print(tabulate(rows, headers=["from", "to", "result"], tablefmt="github"))
    print("  (labels are not identity: anyone who can create pods in the namespace can claim a label; that is what RBAC is for)")

    print("(the cluster is kept; events under load are in phase5_events.py; `kind delete cluster --name ai-lab` removes it)")


if __name__ == "__main__":
    main()
