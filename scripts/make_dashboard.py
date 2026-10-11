"""Generate the Grafana dashboard JSON (observability/grafana/dashboards/ai-system.json).

Panels are listed as data so the same queries can be read, reviewed and tested; the file is generated, not hand-edited.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from deploylab import slo  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
DS = {"type": "prometheus", "uid": "prom"}
_id = [0]


def panel(title, exprs, kind="timeseries", unit="short", x=0, y=0, w=8, h=7, legends=None, mappings=None, thresholds=None):
    _id[0] += 1
    fieldcfg = {"defaults": {"unit": unit}, "overrides": []}
    if mappings:
        fieldcfg["defaults"]["mappings"] = mappings
    if thresholds:
        fieldcfg["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return {"id": _id[0], "title": title, "type": kind, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": fieldcfg,
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": (legends or [""] * len(exprs))[i]} for i, e in enumerate(exprs)]}


slos = slo.load(ROOT / "observability" / "slo.json")
panels = [
    panel("Tickets per minute, by outcome", ['sum by (action) (rate(ai_ticket_outcomes_total[1m])) * 60'], x=0, y=0, legends=["{{action}}"]),
    panel("Safe-completion SLI (5m, released or escalated)", ['1 - slo:error_ratio:ticket_safe_completion:5m'], kind="stat", unit="percentunit", x=8, y=0, w=4,
          thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.9}, {"color": "green", "value": 0.97}]),
    panel("Answer-quality probes correct (last 12)", ['ai_probe_correct_ratio'], kind="stat", unit="percentunit", x=12, y=0, w=4,
          thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.9}, {"color": "green", "value": 0.95}]),
    panel("Drift verdict", ['ai_drift_verdict'], kind="stat", x=16, y=0, w=4,
          mappings=[{"type": "value", "options": {"0": {"text": "stable", "color": "green"}, "1": {"text": "warn", "color": "yellow"}, "2": {"text": "critical", "color": "red"}}}]),
    panel("Restricted facts leaked (15m)", ['sum(increase(ai_probe_leaks_total[15m])) or vector(0)'], kind="stat", x=20, y=0, w=4,
          thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}]),
    panel("Ticket latency p50 / p95 (seconds)", ['histogram_quantile(0.5, sum by (le) (rate(ai_ticket_duration_seconds_bucket[5m])))',
                                                'histogram_quantile(0.95, sum by (le) (rate(ai_ticket_duration_seconds_bucket[5m])))'],
          unit="s", x=0, y=7, legends=["p50", "p95"]),
    panel("Model calls per minute, by route and tier", ['sum by (route, tier) (rate(ai_gateway_requests_total[1m])) * 60'], x=8, y=7, legends=["{{route}} ({{tier}})"]),
    panel("Tokens per minute", ['sum by (direction) (rate(ai_gateway_tokens_total[1m])) * 60'], x=16, y=7, legends=["{{direction}}"]),
    panel("Error-budget burn rate (5m; 1 = budget lasts exactly the SLO period)",
          [f"slo:error_ratio:{s.name}:5m / {s.budget:g}" for s in slos], x=0, y=14, w=12, legends=[s.name for s in slos]),
    panel("Cost, USD per hour", ['sum(rate(ai_gateway_cost_usd_total[5m])) * 3600'], unit="currencyUSD", x=12, y=14, w=6),
    panel("Circuit breaker (0 closed, 1 half-open, 2 open)", ['max by (route) (ai_gateway_breaker_state)'], x=18, y=14, w=6, legends=["{{route}}"]),
    panel("Agent runs and tool calls per minute (emitted by Requisite itself)",
          ['sum by (agent) (rate(requisite_agent_runs_total[1m])) * 60', 'sum by (tool) (rate(requisite_agent_tool_calls_total[1m])) * 60'],
          x=0, y=28, w=12, legends=["runs: {{agent}}", "tool: {{tool}}"]),
    panel("Release serving", ['max by (release, role, digest) (ai_release_serving)'], kind="table", x=0, y=21, w=8),
    panel("Alerts firing", ['sum by (alertname, severity) (ALERTS{alertstate="firing"})'], kind="table", x=8, y=21, w=16),
]
dashboard = {"uid": "ai-system", "title": "AI system", "tags": ["ai", "slo"], "timezone": "browser", "schemaVersion": 39, "version": 1,
             "refresh": "10s", "time": {"from": "now-30m", "to": "now"}, "panels": panels}
out = ROOT / "observability" / "grafana" / "dashboards" / "ai-system.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(dashboard, indent=1), encoding="utf-8")
print("wrote", out, "with", len(panels), "panels")
