"""Rolling a release out safely: activate, canary, automatic rollback, shadow and manual rollback, on real models.

    python -u scripts/phase2_study.py

Starts the production-shaped stack (four processes) on release r1-baseline, then changes releases while tickets
flow. The bad canary here (r4-renamed-categories) is deliberately NOT run through the gate first: it exercises the
second line of defence, the one that exists for the change the gate did not catch.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
from tabulate import tabulate

import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploylab.stack import ROOT, Stack, load_gemini_key  # noqa: E402
from tickets.data import TICKETS  # noqa: E402

NORMAL = [t for t in TICKETS if t.kind == "normal"]


def banner(text):
    print(f"\n{'=' * 8} {text}")


class Ops:
    def __init__(self, stack):
        self.s = stack
        self.h = {"X-Admin-Key": stack.admin_key}

    def login(self, user, aud="ticket-agent"):
        return httpx.post(f"{self.s.urls['identity']}/login", json={"username": user, "audience": aud}, timeout=10).json()["token"]

    def admin(self, service, path, body=None):
        url = f"{self.s.urls[service]}/admin/releases{path}"
        r = httpx.post(url, json=body or {}, headers=self.h, timeout=120) if body is not None else httpx.get(url, headers=self.h, timeout=20)
        return r.status_code, r.json()

    def ticket(self, tid, token, text):
        token = self.login("sam")                 # tokens live 5 minutes; a long study must not reuse one
        t0 = time.time()
        r = httpx.post(f"{self.s.urls['tickets']}/tickets", json={"id": tid, "text": text}, timeout=300,
                       headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": f"p2-{tid}"})
        b = r.json()
        return {"id": tid, "http": r.status_code, "action": b.get("action"), "release": b.get("release", {}).get("id"),
                "role": b.get("release", {}).get("role"), "seconds": round(time.time() - t0, 1), "reasons": b.get("reasons", [])}

    def stream(self, prefix, n, token):
        out = []
        for i in range(n):
            t = NORMAL[i % len(NORMAL)]
            out.append(self.ticket(f"{prefix}{i + 1:02d}", token, t.text))
        return out


def show(rows):
    print(tabulate([[r["id"], r["http"], r["action"], r["release"], r["role"], f"{r['seconds']}s"] for r in rows],
                   headers=["ticket", "HTTP", "action", "served by", "role", "time"], tablefmt="github"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", default="1,2,3,4,5,6,7", help="comma list; 5 and 6 can be run alone")
    want = {int(x) for x in ap.parse_args().sections.split(",")}
    key = load_gemini_key()
    run_dir = ROOT / "run-p2"
    shutil.rmtree(run_dir, ignore_errors=True)
    stack = Stack(run_dir=run_dir, gemini_key=key, pin_light=True, release="r1-baseline", version="phase2")
    stack.start()
    try:
        print("ready:", stack.wait_ready())
        ops = Ops(stack)
        token = ops.login("sam")

        if 1 in want:
            banner("1. the admin API refuses anyone without the key; a release that fails lint never loads")
            print("no key:", httpx.get(f"{stack.urls['tickets']}/admin/releases", timeout=10).status_code,
                  "| wrong key:", httpx.get(f"{stack.urls['tickets']}/admin/releases", headers={"X-Admin-Key": "x" * 24}, timeout=10).status_code)
            code, body = ops.admin("tickets", "/load", {"id": "r9-does-not-exist"})
            print("load a release that is not in the registry:", code, body.get("detail"))
            code, body = ops.admin("tickets", "/load", {"id": "r2-shorter-reply"})
            print("load r2-shorter-reply:", code, body)
            st = ops.admin("tickets", "")[1]
            print("loaded:", {k: v["digest"] for k, v in st["loaded"].items()}, "| active:", st["active"])

        if 2 in want:
            banner("2. activate r2, then roll back: one call each, no restart")
            before = ops.ticket("A01", token, NORMAL[0].text)
            print("before:", before["release"], before["action"])
            t0 = time.time()
            code, st = ops.admin("tickets", "/activate", {"id": "r2-shorter-reply"})
            print(f"activate took {time.time() - t0:.2f}s; active now {st['active']}, previous {st['previous']}")
            mid = ops.ticket("A02", token, NORMAL[0].text)
            print("after activate:", mid["release"], mid["action"])
            t0 = time.time()
            code, st = ops.admin("tickets", "/rollback", {"reason": "demonstration"})
            print(f"rollback took {time.time() - t0:.2f}s; active now {st['active']}")
            after = ops.ticket("A03", token, NORMAL[0].text)
            print("after rollback:", after["release"], after["action"])

        if 3 in want:
            banner("3. a healthy canary: r2 at 30 percent of tickets")
            ops.admin("tickets", "/canary", {"id": "r2-shorter-reply", "percent": 30})
            rows = ops.stream("B", 12, token)
            show(rows)
            st = ops.admin("tickets", "")[1]
            print("canary still on:", st["canary"], "| window:", st["window"])

        if 4 in want:
            banner("4. a bad canary: r4-renamed-categories at 40 percent, never gated")
            ops.admin("tickets", "/rollback", {"reason": "end healthy canary"})
            code, body = ops.admin("tickets", "/load", {"id": "r4-renamed-categories"})
            print("loaded r4:", body)
            ops.admin("tickets", "/canary", {"id": "r4-renamed-categories", "percent": 40})
            rows = ops.stream("C", 16, token)
            show(rows)
            st = ops.admin("tickets", "")[1]
            bad = [r for r in rows if r["release"] == "r4-renamed-categories"]
            print(f"\ncustomers served by the bad release before it was removed: {len(bad)} (all held: {all(r['action'] == 'held' for r in bad)})")
            print("canary now:", st["canary"], "| active:", st["active"])
            for ev in st["events"]:
                if ev["type"] == "auto_rollback":
                    print("EVENT auto_rollback:", ev["reason"])
            after = ops.stream("E", 3, token)
            print("three more tickets after the automatic rollback were served by:", sorted({r["release"] for r in after}), [r["action"] for r in after])

        if 5 in want:
            banner("5. shadow: r2 sees a copy of active traffic, callers never see it")
            ops.admin("tickets", "/load", {"id": "r2-shorter-reply"})
            ops.admin("tickets", "/shadow", {"id": "r2-shorter-reply"})
            rows = ops.stream("D", 6, token)
            print("served by:", sorted({r["release"] for r in rows}), "| roles:", sorted({r["role"] for r in rows}))
            for _ in range(60):
                if ops.admin("tickets", "")[1]["shadow_stats"]["n"] >= 6:
                    break
                time.sleep(2)
            print("shadow comparison:", ops.admin("tickets", "")[1]["shadow_stats"])
            ops.admin("tickets", "/shadow", {"id": None})

        if 6 in want:
            banner("6. the knowledge release: a bad answerer goes live, then one call undoes it")
            import asyncio
            from a2a_layer.client import KnowledgeClient
            from deploylab.gate import normalize
            ask = lambda q, _tok=None: normalize(asyncio.run(KnowledgeClient(stack.urls["knowledge"]).ask(q, ops.login("alice", "knowledge-agent"), "p2-know", subject="alice")))
            q = "Which cafeteria vendor supplies the Berlin office lunch menu?"
            print("r1-baseline answers:", ask(q)[:110])
            ops.admin("knowledge", "/load", {"id": "r3-helpful-answerer"})
            ops.admin("knowledge", "/activate", {"id": "r3-helpful-answerer"})
            print("r3-helpful-answerer answers:", ask(q)[:200].replace("\n", " "))
            ops.admin("knowledge", "/rollback", {"reason": "answers drifted"})
            print("after rollback:", ask(q)[:110])
            print("knowledge release history:", [e["type"] + ":" + str(e.get("release")) for e in ops.admin("knowledge", "")[1]["events"]])

        if 7 in want:
            banner("7. what the release set recorded")
            st = ops.admin("tickets", "")[1]
            print(tabulate([[rid, v["digest"], v["n"], v["bad_rate"], v["p50_s"]] for rid, v in st["loaded"].items()],
                           headers=["release", "digest", "requests", "bad rate", "p50 s"], tablefmt="github"))
            print("events:")
            for ev in st["events"]:
                print("  ", {k: v for k, v in ev.items() if k != "at"})
    finally:
        print("\nstopping:", json.dumps(stack.stop()))


if __name__ == "__main__":
    main()
