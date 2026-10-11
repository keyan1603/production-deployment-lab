"""Service level objectives as data, and the Prometheus rules generated from them.

An SLO is "this fraction of events must be good over the long run". The alert fires on how fast the error budget is
burning, not on a single bad minute (the multi-window, multi-burn-rate method from the Google SRE workbook):

    burn rate = error ratio / (1 - objective)        1.0 means the budget lasts exactly the SLO period

    page    burn > 14.4 over the long window AND the short window    (2 percent of a 30-day budget in an hour)
    ticket  burn > 6    over the long window AND the short window    (5 percent of the budget in six hours)

Requiring both windows makes the alert fast to fire and fast to clear: the long window proves it is not a blip, the
short one proves it is still happening. A traffic floor stops a quiet period from turning one failed request into a
100 percent error ratio.

The SLIs here are not only latency and availability. `ticket_safe_completion` counts a held ticket as a failure and
`answer_quality_probe` counts a wrong answer to a known question as one, because a system can be fast and up while
being wrong.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

# (long window, short window, burn-rate threshold, severity, for)
STANDARD = (("1h", "5m", 14.4, "page", "2m"), ("6h", "30m", 6.0, "ticket", "15m"))
# Minutes instead of hours, so an alert can be seen to fire and clear within a short live run. Same method, same thresholds.
LAB = (("5m", "1m", 14.4, "page", "30s"), ("15m", "3m", 6.0, "ticket", "1m"))
RECORD_WINDOWS = ("1m", "3m", "5m", "15m", "30m", "1h", "6h")


@dataclass(frozen=True)
class SLO:
    name: str
    description: str
    objective: float
    good: str            # PromQL with $W where the range window goes (PromQL uses braces itself)
    total: str
    min_rate: float = 0.0     # events per second below which the alert stays quiet (too little traffic to judge)

    @property
    def budget(self) -> float:
        return 1.0 - self.objective

    def ratio(self, window: str) -> str:
        return f"(1 - (({self.good.replace("$W", window)}) / ({self.total.replace("$W", window)})))"


def load(path: Path) -> list[SLO]:
    return [SLO(**s) for s in json.loads(Path(path).read_text(encoding="utf-8"))["slos"]]


def burn_expr(slo: SLO, window: str) -> str:
    return f"slo:error_ratio:{slo.name}:{window} / {slo.budget:g}"


def build_rules(slos: list[SLO], windows=STANDARD) -> dict:
    """The Prometheus rule file as a dict: recording rules for each SLO and window, then the burn-rate alerts."""
    record, alerts = [], []
    needed = sorted({w for pair in windows for w in pair[:2]}, key=RECORD_WINDOWS.index)
    for slo in slos:
        for w in needed:
            record.append({"record": f"slo:error_ratio:{slo.name}:{w}",
                           "expr": f"{slo.ratio(w)} and on() (({slo.total.replace("$W", w)}) > {slo.min_rate:g})"})
        for long_w, short_w, burn, severity, hold in windows:
            alerts.append({
                "alert": f"SLOBurn_{slo.name}_{severity}",
                "expr": f"({burn_expr(slo, long_w)} > {burn:g}) and ({burn_expr(slo, short_w)} > {burn:g})",
                "for": hold,
                "labels": {"severity": severity, "slo": slo.name},
                "annotations": {"summary": f"{slo.name} is burning its error budget {burn:g}x too fast ({long_w} and {short_w})",
                                "description": f"{slo.description}. Objective {slo.objective:.1%}."}})
    alerts += [
        {"alert": "RestrictedFactLeaked", "expr": "increase(ai_probe_leaks_total[15m]) > 0", "for": "0s",
         "labels": {"severity": "page", "slo": "access_control"},
         "annotations": {"summary": "A synthetic probe found a restricted fact in an answer",
                         "description": "Access control failed for at least one probe in the last 15 minutes. The objective is zero."}},
        {"alert": "DriftCritical", "expr": "ai_drift_verdict == 2", "for": "1m", "labels": {"severity": "page", "slo": "drift"},
         "annotations": {"summary": "The drift monitor reads critical against the saved baseline",
                         "description": "See the monitor's /state for the findings (quality, leaks, degraded share)."}},
        {"alert": "CircuitBreakerOpen", "expr": "max by (route) (ai_gateway_breaker_state) == 2", "for": "1m",
         "labels": {"severity": "ticket", "slo": "dependencies"},
         "annotations": {"summary": "A model route's circuit breaker has been open for a minute",
                         "description": "Requests are failing over to another route."}},
        {"alert": "DegradedTierServing", "expr": 'sum(rate(ai_gateway_requests_total{tier="degraded"}[5m])) > 0', "for": "1m",
         "labels": {"severity": "page", "slo": "no_degraded_tier"},
         "annotations": {"summary": "Answers are being produced by the degraded-tier model",
                         "description": "They are held or labelled, but a person must know."}},
    ]
    return {"groups": [{"name": "slo-recording", "interval": "15s", "rules": record}, {"name": "slo-alerts", "interval": "15s", "rules": alerts}]}


def dump(rules: dict) -> str:
    return "# Generated by scripts/make_slo_rules.py from observability/slo.json. Do not edit by hand.\n" + yaml.safe_dump(rules, sort_keys=False, width=140)
