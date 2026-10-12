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


RUNBOOKS = {
    "ticket_availability": "ticket-availability", "ticket_safe_completion": "tickets-held", "ticket_latency": "ticket-latency",
    "answer_quality_probe": "answer-quality", "no_degraded_tier": "degraded-tier",
}


# What a ticket costs on the light model, measured with the system design lab's runs (USD). The cost alert fires at three times this.
EXPECTED_COST_PER_TICKET = 0.0011


def build_rules(slos: list[SLO], windows=STANDARD, expected_cost: float = EXPECTED_COST_PER_TICKET) -> dict:
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
                "annotations": {"runbook": f"docs/runbooks/{RUNBOOKS.get(slo.name, 'incident-process')}.md", "summary": f"{slo.name} is burning its error budget {burn:g}x too fast ({long_w} and {short_w})",
                                "description": f"{slo.description}. Objective {slo.objective:.1%}."}})
    ops = [
        ("RestrictedFactLeaked", "increase(ai_probe_leaks_total[15m]) > 0", "0s", "page", "access_control", "restricted-fact-leaked",
         "A synthetic probe found a restricted fact in an answer", "Access control failed for at least one probe in the last 15 minutes. The objective is zero."),
        ("DriftCritical", "max(max_over_time(ai_drift_verdict[3m])) == 2", "1m", "page", "drift", "answer-quality",
         "The drift monitor has read critical against the saved baseline in the last three minutes",
         "The look-back is hysteresis: the verdict is computed over a sliding window and can flip between probes, which without it pages and resolves in turn. See the monitor's /state for the findings."),
        ("CircuitBreakerOpen", "max by (route) (max_over_time(ai_gateway_breaker_state[2m])) == 2", "1m", "ticket", "dependencies", "provider-trouble",
         "A model route's circuit breaker has been tripping for a minute",
         "A breaker re-closes to half-open every 30 s to test the route, so its state alternates while the route is down; the look-back sees the trips. Requests are failing over to another route."),
        ("DegradedTierServing", 'sum(rate(ai_gateway_requests_total{tier="degraded"}[5m])) > 0', "1m", "page", "no_degraded_tier", "degraded-tier",
         "Answers are being produced by the degraded-tier model", "They are held or labelled, but a person must know."),
        ("QuotaNearlyExhausted", "max by (model) (ai_gateway_quota_used) > 0.8", "1m", "ticket", "quota", "quota",
         "A model is past 80 percent of its daily allowance", "The quota guard will start refusing the lowest-priority caller at its threshold."),
        ("QuotaGuardShedding", 'sum(increase(ai_gateway_shed_total{reason="quota"}[5m])) > 0', "0s", "page", "quota", "quota",
         "The gateway is refusing requests to protect the daily allowance", "Tickets are being held with a clear reason instead of failing over to a weaker model."),
        ("DailyBudgetNearlyExhausted", "max(ai_gateway_budget_used) > 0.8", "1m", "ticket", "cost", "cost",
         "More than 80 percent of today's spend budget is used", "Callers other than the exempt ones are refused at 100 percent."),
        ("BudgetShedding", 'sum(increase(ai_gateway_shed_total{reason="budget"}[5m])) > 0', "0s", "page", "cost", "cost",
         "The gateway is refusing requests because today's budget is spent", "Tickets are held with a budget reason."),
        ("CostPerTicketHigh", f"(sum(rate(ai_gateway_cost_usd_total[5m])) / sum(rate(ai_ticket_outcomes_total[5m]))) > {3 * expected_cost:g} and sum(rate(ai_ticket_outcomes_total[5m])) > 0.02",
         "2m", "ticket", "cost", "cost", f"Each ticket is costing more than three times the expected ${expected_cost:g}",
         "A longer prompt, a loop, retries or a model or tier change is the usual cause. The expected cost is a calibration: update it when the system changes on purpose."),
        ("TicketsFrozen", "max(ai_ticket_frozen) == 1", "0s", "ticket", "operations", "incident-process",
         "The ticket service is frozen: no new tickets are being accepted", "Someone used the kill switch. Make sure it is turned off when the cause is fixed."),
    ]
    alerts += [{"alert": name, "expr": expr, "for": hold, "labels": {"severity": sev, "slo": slo_label},
                "annotations": {"runbook": f"docs/runbooks/{book}.md", "summary": summary, "description": description}}
               for name, expr, hold, sev, slo_label, book, summary, description in ops]
    return {"groups": [{"name": "slo-recording", "interval": "15s", "rules": record}, {"name": "slo-alerts", "interval": "15s", "rules": alerts}]}


def dump(rules: dict) -> str:
    return "# Generated by scripts/make_slo_rules.py from observability/slo.json. Do not edit by hand.\n" + yaml.safe_dump(rules, sort_keys=False, width=140)
