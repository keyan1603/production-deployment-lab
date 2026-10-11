"""The dashboards, SLOs and alert rules may only reference metrics the code really emits.

A typo in a PromQL expression does not fail anywhere until the alert that should have fired stays silent. This test turns
that into a failure now: it finds every instrument the services create, converts each OpenTelemetry name to the name
Prometheus will expose (the collector's conversion: dots to underscores, `_total` on counters, `_seconds` and the
`_bucket`/`_sum`/`_count` family on histograms), and checks every metric a dashboard panel, an SLO or a rule mentions.
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES = list((ROOT / "services").glob("*.py")) + list((ROOT / "deploylab").glob("*.py"))


def emitted() -> set:
    names = set()
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        for name in re.findall(r'\.inc\(\s*"([^"]+)"', text):
            names.add(name.replace(".", "_") + "_total")
        for name in re.findall(r'\.observe\(\s*"([^"]+)"', text):
            base = name.replace(".", "_") + "_seconds"
            names |= {base + suffix for suffix in ("_bucket", "_sum", "_count")}
        for name in re.findall(r'\.gauge\(\s*"([^"]+)"', text):
            names.add(name.replace(".", "_"))
    # Emitted by the Requisite framework itself (requisite/agents/agent.py, requisite/ai.py); reused, not re-implemented.
    names |= {"requisite_agent_runs_total", "requisite_agent_tool_calls_total", "requisite_agent_run_duration_seconds_bucket",
              "requisite_ai_requests_total", "requisite_ai_tokens_total"}
    names.add("ALERTS")                                        # Prometheus's own series for alerts
    return names


def metrics_in(expr: str) -> set:
    expr = re.sub(r'"[^"]*"', '""', expr)                      # drop label values
    expr = re.sub(r"\b(by|without|on|ignoring)\s*\([^)]*\)", "", expr)
    return set(re.findall(r"\b(ai_[a-z_]+|http_server_[a-z_]+|requisite_[a-z_]+)\b", expr))


def test_the_code_emits_what_we_think_it_emits():
    names = emitted()
    for expected in ("ai_ticket_outcomes_total", "ai_gateway_requests_total", "ai_ticket_duration_seconds_bucket", "ai_drift_verdict",
                     "ai_probe_results_total", "http_server_requests_total", "ai_gateway_breaker_state", "ai_release_serving"):
        assert expected in names, expected


def test_every_dashboard_query_uses_a_real_metric():
    known = emitted()
    board = json.loads((ROOT / "observability" / "grafana" / "dashboards" / "ai-system.json").read_text(encoding="utf-8"))
    for panel in board["panels"]:
        for target in panel["targets"]:
            unknown = metrics_in(target["expr"]) - known
            assert not unknown, f"{panel['title']}: {sorted(unknown)}"


def test_every_slo_and_rule_uses_a_real_metric():
    known = emitted()
    slos = json.loads((ROOT / "observability" / "slo.json").read_text(encoding="utf-8"))["slos"]
    for s in slos:
        for expr in (s["good"], s["total"]):
            assert not (metrics_in(expr) - known), (s["name"], sorted(metrics_in(expr) - known))
    for name in ("slo.rules.yml", "slo.lab.rules.yml"):
        groups = yaml.safe_load((ROOT / "observability" / "prometheus" / "rules" / name).read_text(encoding="utf-8"))["groups"]
        for g in groups:
            for rule in g["rules"]:
                unknown = metrics_in(rule["expr"]) - known
                assert not unknown, (rule.get("alert") or rule.get("record"), sorted(unknown))


def test_slo_label_values_match_what_the_services_emit():
    """The SLO queries select on label values (service_name, path, status_class, tier, action, result); these must exist in the code."""
    text = "".join(p.read_text(encoding="utf-8") for p in SOURCES)
    for fragment in ("released", "escalated", "degraded", "result", "status_class", "case_kind"):
        assert fragment in text, fragment
    assert "// 100}xx" in (ROOT / "deploylab" / "runtime.py").read_text(encoding="utf-8")        # status classes are "2xx", "5xx", ...
    assert 'service.name' in (ROOT / "deploylab" / "telemetry.py").read_text(encoding="utf-8")
    assert '"/tickets"' in (ROOT / "deploylab" / "telemetry.py").read_text(encoding="utf-8")
