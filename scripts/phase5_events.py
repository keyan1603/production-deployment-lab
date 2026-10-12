"""Platform events under load, measured properly: rolling updates (repeated), pod crashes and a node drain.

    python -u scripts/phase5_events.py

Needs the cluster from phase5_cluster.py. SIMULATED PROVIDER (see that script). Two measures run through every event:
the tickets that fail or are held (closed-loop clients through the NodePort), and a probe of the ticket service's /readyz every
half second, which gives the true length of any stretch in which the service was not ready. The platform's own "rolled out"
message is not used to decide when something has recovered: it can report success before the controller has noticed a crash.
"""

import json
import sys
import threading
import time
from pathlib import Path

import httpx
from tabulate import tabulate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.phase5_cluster import Load, banner, k, pods, TICKETS_URL, CTX, ROOT  # noqa: E402
import subprocess  # noqa: E402


class Readiness:
    """Polls the ticket service's /readyz through the NodePort and keeps every unready stretch."""

    def __init__(self):
        self.samples, self.stop_flag = [], threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stop_flag.is_set():
            t = time.time()
            try:
                code = httpx.get(f"{TICKETS_URL}/readyz", timeout=2).status_code
            except httpx.HTTPError:
                code = 0
            self.samples.append((t, code))
            self.stop_flag.wait(0.5)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.stop_flag.set()
        self.thread.join(timeout=5)

    def gaps(self, a, b):
        """Lengths in seconds of every stretch in [a, b] where a probe did not get a ready answer."""
        out, begin = [], None
        for t, code in self.samples:
            if not a <= t <= b:
                continue
            if code != 200 and begin is None:
                begin = t
            elif code == 200 and begin is not None:
                out.append(round(t - begin, 1))
                begin = None
        if begin is not None:
            out.append(round(b - begin, 1))
        return out


def settled(name, replicas, timeout=240):
    """True once exactly `replicas` pods exist, none is terminating and all are Ready: the real end of a rollout or a recovery."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        items = json.loads(k("get", "pods", "-l", f"app.kubernetes.io/name={name}", "-o", "json") or "{}").get("items", [])
        live = [p for p in items if "deletionTimestamp" not in p["metadata"]]
        ready = [p for p in live if all(c.get("ready") for c in p.get("status", {}).get("containerStatuses", [{"ready": False}]))]
        if len(live) == replicas == len(ready) and len(items) == len(live):
            return round(time.time() - t0, 1)
        time.sleep(0.5)
    return None


def event(label, action, load, probe, before=20, after=25):
    time.sleep(before)
    t0 = time.time()
    detail = action()
    t1 = time.time()
    time.sleep(after)
    w = load.window(t0 - 3, time.time())
    gaps = probe.gaps(t0 - 1, t1 + after)
    print(f"[{label}] recovered after {detail}s; tickets {w['tickets']}: released {w['released']}, held {w['held']}, failed {w['failed']} {json.dumps(w['how'])}; "
          f"service not ready for {gaps or 'no time at all'} seconds")
    return label, detail, w, gaps


def restart_many(name, replicas, times, gap_s=25):
    def run():
        total = 0.0
        for i in range(times):
            k("rollout", "restart", f"deployment/{name}")
            total += settled(name, replicas) or 0
            time.sleep(gap_s)
        return round(total / times, 1)
    return run


def main():
    proper_only = "--proper-only" in sys.argv                    # re-measure just the rolling update with the manifests' settings
    summary = []
    banner("A. rolling updates under load, five restarts each, naive settings against the manifests' settings")
    for variant in (("proper",) if proper_only else ("naive", "proper")):
        if variant == "naive":
            k("patch", "deployment", "tickets", "--type=json", "-p", json.dumps([{"op": "remove", "path": "/spec/template/spec/containers/0/readinessProbe"},
                                                                              {"op": "replace", "path": "/spec/template/spec/terminationGracePeriodSeconds", "value": 1}]))
            k("set", "env", "deployment/tickets", "DRAIN_SECONDS=0")
        else:
            k("apply", "-f", str(ROOT / "deploy" / "k8s" / "13-tickets.yaml"))
        settled("tickets", 2)
        load, probe = Load().start(), Readiness().start()
        label = {"naive": "5 restarts, no readiness gate, no drain, 1 s grace", "proper": "5 restarts, readiness gate, drain, 75 s grace"}[variant]
        try:
            summary.append(event(label, restart_many("tickets", 2, 5), load, probe, before=15, after=15))
        finally:
            load.stop()
            probe.stop()

    if proper_only:
        print(tabulate([[label, detail, w["tickets"], w["released"], w["held"], w["failed"], json.dumps(w["how"]), ", ".join(str(g) for g in gaps) or "-"] for label, detail, w, gaps in summary],
                       headers=["event under load", "mean recovery s", "tickets", "released", "held", "failed", "how they failed", "service unready, seconds"], tablefmt="github"))
        return
    banner("B. crashes: a pod killed with no grace (SIGKILL), three times each")

    def crash(name, replicas, times):
        def run():
            total = 0.0
            for _ in range(times):
                victim = pods(name)[0][0]
                k("delete", "pod", victim, "--grace-period=0", "--force")
                total += settled(name, replicas) or 0
                time.sleep(15)
            return round(total / times, 1)
        return run

    for name, replicas, label in (("tickets", 2, "ticket pod killed (2 replicas)"), ("gateway", 1, "gateway pod killed (the only one)")):
        load, probe = Load().start(), Readiness().start()
        try:
            summary.append(event(label, crash(name, replicas, 3), load, probe, before=15, after=20))
        finally:
            load.stop()
            probe.stop()

    banner("C. draining the node that hosts the gateway")
    node = pods("gateway")[0][1]
    print("  gateway on", node, "| tickets on", [p[1].replace("ai-lab-", "") for p in pods("tickets")])

    def drain():
        t0 = time.time()
        out = k("drain", node, "--ignore-daemonsets", "--delete-emptydir-data", "--timeout=180s", timeout=240)
        print("  drain:", " | ".join(out.splitlines()[-2:])[:200])
        settled("gateway", 1)
        settled("tickets", 2)
        return round(time.time() - t0, 1)

    load, probe = Load().start(), Readiness().start()
    try:
        summary.append(event("node drain", drain, load, probe, before=15, after=20))
    finally:
        load.stop()
        probe.stop()
        subprocess.run(["kubectl", "--context", CTX, "uncordon", node], capture_output=True, text=True)
    print("  pods after:", {n: [p[1].replace("ai-lab-", "") for p in pods(n)] for n in ("gateway", "tickets", "knowledge")})

    banner("summary")
    print(tabulate([[label, detail, w["tickets"], w["released"], w["held"], w["failed"], json.dumps(w["how"]), ", ".join(str(g) for g in gaps) or "-"]
                    for label, detail, w, gaps in summary],
                   headers=["event under load", "mean recovery s", "tickets", "released", "held", "failed", "how they failed", "service unready, seconds"], tablefmt="github"))


if __name__ == "__main__":
    main()
