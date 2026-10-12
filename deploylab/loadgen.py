"""A small closed-loop load generator for the ticket API, and a controller for the provider simulator.

Closed loop: each of N workers sends a ticket, waits for the answer, and sends the next, so offered load adapts to how fast the system
answers (like a fixed pool of operators). That is the right shape for finding where throughput stops growing and latency starts to.
"""

from __future__ import annotations

import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent


# ---- the simulator -----------------------------------------------------------------------------
class Sim:
    def __init__(self, port: int = 8900, rpm: int = 60, rpd: int = 100000, log: Optional[Path] = None) -> None:
        self.port, self.url = port, f"http://127.0.0.1:{port}"
        try:
            httpx.get(f"{self.url}/healthz", timeout=1)
        except httpx.HTTPError:
            pass
        else:                                  # something already answers there: a leftover simulator with other limits would silently invalidate a run
            raise RuntimeError(f"port {port} is already in use; stop the other process first")
        out = open(log, "ab") if log else subprocess.DEVNULL
        self.proc = subprocess.Popen([sys.executable, "-m", "sim.gemini_sim", "--port", str(port), "--rpm", str(rpm), "--rpd", str(rpd)], cwd=ROOT,
                                     stdout=out, stderr=subprocess.STDOUT)
        for _ in range(100):
            try:
                if httpx.get(f"{self.url}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("simulator did not start")

    def faults(self, mode: str, **kw) -> dict:
        return httpx.post(f"{self.url}/_sim/faults", json={"mode": mode, **kw}, timeout=5).json()

    def config(self, **kw) -> dict:
        return httpx.post(f"{self.url}/_sim/config", json=kw, timeout=5).json()

    def stats(self) -> dict:
        return httpx.get(f"{self.url}/_sim/stats", timeout=5).json()

    def reset(self) -> None:
        httpx.post(f"{self.url}/_sim/reset", timeout=5)

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


# ---- the load generator ------------------------------------------------------------------------
@dataclass
class Result:
    ticket: str
    url: str
    http: int
    action: str
    seconds: float
    reasons: list = field(default_factory=list)
    error: str = ""


@dataclass
class Run:
    results: list
    wall_s: float

    def summary(self) -> dict:
        ok = [r for r in self.results if r.http == 200]
        lat = sorted(r.seconds for r in ok)
        pct = lambda p: round(lat[min(len(lat) - 1, int(p * len(lat)))], 2) if lat else None
        actions: dict = {}
        for r in ok:
            actions[r.action] = actions.get(r.action, 0) + 1
        codes: dict = {}
        for r in self.results:
            codes[r.http] = codes.get(r.http, 0) + 1
        return {"sent": len(self.results), "answered": len(ok), "wall_s": round(self.wall_s, 1),
                "tickets_per_min": round(len(ok) / self.wall_s * 60, 1) if self.wall_s else 0.0,
                "p50_s": pct(0.5), "p95_s": pct(0.95), "max_s": round(lat[-1], 1) if lat else None,
                "mean_s": round(statistics.mean(lat), 2) if lat else None, "http": codes, "actions": actions}


def run_load(urls: list, workers: int, per_worker: int, token_for: Callable[[], str], texts: list, prefix: str = "L",
             timeout: float = 300.0, on_result: Optional[Callable[[Result], None]] = None) -> Run:
    """`workers` closed-loop clients, each sending `per_worker` tickets, spread across `urls` round-robin."""
    results: list = []
    lock = threading.Lock()
    counter = [0]

    def worker(wid: int) -> None:
        for _ in range(per_worker):
            with lock:
                counter[0] += 1
                n = counter[0]
            url = urls[n % len(urls)]
            tid = f"{prefix}{n:04d}"
            t0 = time.perf_counter()
            try:
                r = httpx.post(f"{url}/tickets", json={"id": tid, "text": texts[n % len(texts)]}, timeout=timeout,
                               headers={"Authorization": f"Bearer {token_for()}", "X-Correlation-Id": f"load-{tid}"})
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                res = Result(tid, url, r.status_code, body.get("action", ""), time.perf_counter() - t0, body.get("reasons", []))
            except httpx.HTTPError as exc:
                res = Result(tid, url, 0, "", time.perf_counter() - t0, error=type(exc).__name__)
            with lock:
                results.append(res)
            if on_result:
                on_result(res)

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return Run(results, time.perf_counter() - t0)
