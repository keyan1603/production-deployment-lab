"""Continuous quality and drift monitoring for the running system.

Latency and error rate say whether the system is answering; they do not say whether it is answering well. This module
asks a fixed set of labelled questions on a schedule (synthetic probes, as a user of the knowledge assistant would
ask them), judges every answer with the same judge the release gate uses, and reduces the last few results plus the
gateway's own audit counters to the metrics the #17 drift monitor watches. The result is exported as OpenTelemetry
metrics, which the alert rules and dashboards read.

    probes           the cases below, one per tick, in rotation: answers that must come back, restricted questions
                     that must not (a leak), a question with no answer, and an injection
    drift verdict    0 stable, 1 warn, 2 critical, from `monitoring.drift.check_drift` against a baseline that a person
                     saved deliberately (scripts/save_baseline.py). Never saved by the monitor itself: a regression
                     saved automatically becomes the new normal instead of an alert.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from deploylab import logs
from deploylab.gate import judge_case
from deploylab.telemetry import telemetry
from knowledge.corpus import CASES
from monitoring.drift import DEFAULT_RULES, check_drift

log = logging.getLogger("monitor")
PROBE_IDS = ("K01", "K02", "K03", "K04", "K12", "K13")      # 2 answers, 2 restricted, 1 unknown, 1 injection
VERDICT = {"stable": 0, "no_baseline": 0, "warn": 1, "critical": 2}


@dataclass
class ProbeResult:
    case_id: str
    expect: str
    ok: bool
    leaked: bool
    error: str = ""
    seconds: float = 0.0


@dataclass
class State:
    results: deque = field(default_factory=lambda: deque(maxlen=12))
    snapshots: deque = field(default_factory=lambda: deque(maxlen=10))
    verdict: dict = field(default_factory=lambda: {"status": "no_baseline", "findings": []})
    metrics: dict = field(default_factory=dict)
    ticks: int = 0
    leaks_total: int = 0


class Monitor:
    """`ask(case)` returns the answer text for a case (raising on a transport failure); `gateway_status()` returns
    the gateway's /v1/status payload. Both are injected so the logic is testable without a network."""

    def __init__(self, ask: Callable, gateway_status: Callable[[], dict], baseline_path: Path, cases=None,
                 min_results: int = 6, clock: Callable[[], float] = time.time) -> None:
        by_id = {c.id: c for c in CASES}
        self.cases = list(cases) if cases is not None else [by_id[i] for i in PROBE_IDS]
        self.ask, self.gateway_status, self.baseline_path, self.min_results, self.clock = ask, gateway_status, Path(baseline_path), min_results, clock
        self.state = State()
        self._next = 0
        self._lock = threading.Lock()
        tel = telemetry()
        tel.gauge("ai.probe.correct_ratio", lambda: [(self.correct_ratio(), {})] if self.state.results else [])
        tel.gauge("ai.drift.verdict", lambda: [(VERDICT[self.state.verdict["status"]], {})])
        tel.gauge("ai.drift.finding", lambda: [(1, {"metric": f["metric"], "severity": f["severity"]}) for f in self.state.verdict["findings"]])

    # -- probes -----------------------------------------------------------------------------------
    def correct_ratio(self) -> float:
        r = list(self.state.results)
        return sum(1 for x in r if x.ok) / len(r) if r else 1.0

    def probe_once(self) -> ProbeResult:
        case = self.cases[self._next % len(self.cases)]
        self._next += 1
        t0 = time.perf_counter()
        try:
            ok, leaked = judge_case(case, self.ask(case))
            res = ProbeResult(case.id, case.expect, ok, leaked, seconds=time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001 - a probe that cannot run is a failed probe, not a crash of the monitor
            res = ProbeResult(case.id, case.expect, False, False, f"{type(exc).__name__}: {str(exc)[:80]}", time.perf_counter() - t0)
        tel = telemetry()
        verdict = "error" if res.error else ("pass" if res.ok else "fail")
        tel.inc("ai.probe.results", case_kind=case.expect, result=verdict)
        tel.observe("ai.probe.duration", res.seconds, case_kind=case.expect)
        if res.leaked:
            tel.inc("ai.probe.leaks")
            self.state.leaks_total += 1
        logs.event(log, "probe", case=case.id, expect=case.expect, result=verdict, leaked=res.leaked, error=res.error)
        with self._lock:
            self.state.results.append(res)
            self.state.ticks += 1
        return res

    # -- drift ------------------------------------------------------------------------------------
    def drift_metrics(self) -> Optional[dict]:
        """The #17 drift metrics for the recent window, or None until there are enough probe results."""
        results = list(self.state.results)
        if len(results) < self.min_results:
            return None
        status = self.gateway_status()
        audit = status.get("audit", {})
        degraded = {r["name"] for r in status.get("routes", []) if r.get("tier") == "degraded"}
        self.state.snapshots.append({"requests": audit.get("requests", 0), "fallbacks": audit.get("fallbacks", 0),
                                     "cost": audit.get("total_cost_usd", 0.0),
                                     "degraded": sum(n for route, n in audit.get("by_route", {}).items() if route in degraded)})
        first, last = self.state.snapshots[0], self.state.snapshots[-1]
        reqs = max(last["requests"] - first["requests"], 1)
        answers = [r for r in results if r.expect == "answer"]
        return {"correct_rate": round(sum(r.ok for r in results) / len(results), 3),
                "authorized_correct_rate": round(sum(r.ok for r in answers) / max(len(answers), 1), 3),
                "leaks": sum(r.leaked for r in results),
                "fallback_rate": round((last["fallbacks"] - first["fallbacks"]) / reqs, 3),
                "degraded_share": round((last["degraded"] - first["degraded"]) / reqs, 3),
                "cost_per_request_usd": round((last["cost"] - first["cost"]) / reqs, 6),
                "p50_latency_ms": audit.get("p50_latency_ms_uncached") or 0.0}

    def evaluate_drift(self) -> dict:
        metrics = self.drift_metrics()
        if metrics is None:
            return self.state.verdict
        verdict = check_drift(metrics, self.baseline_path, DEFAULT_RULES)
        self.state.metrics, self.state.verdict = metrics, verdict
        if verdict["status"] not in ("stable", "no_baseline"):
            logs.event(log, "drift", level=logging.WARNING, status=verdict["status"], findings=[f["metric"] for f in verdict["findings"]])
        return verdict

    def tick(self) -> dict:
        self.probe_once()
        return self.evaluate_drift()

    def snapshot(self) -> dict:
        return {"ticks": self.state.ticks, "correct_ratio": round(self.correct_ratio(), 3), "leaks_total": self.state.leaks_total,
                "verdict": self.state.verdict["status"], "findings": self.state.verdict["findings"], "metrics": self.state.metrics,
                "recent": [{"case": r.case_id, "ok": r.ok, "leaked": r.leaked, "error": r.error} for r in self.state.results]}
