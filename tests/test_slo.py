"""Offline tests for SLO rule generation: the burn-rate maths, the windows, and that Prometheus itself accepts the rules."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from deploylab import slo

ROOT = Path(__file__).resolve().parent.parent
SLOS = slo.load(ROOT / "observability" / "slo.json")
PODMAN = shutil.which("podman") or r"C:\Program Files\RedHat\Podman\podman.exe"


def test_budget_and_burn_rate_arithmetic():
    s = slo.SLO("x", "d", 0.99, "g", "t")
    assert s.budget == pytest.approx(0.01)
    # an error ratio of 14.4 percent against a 1 percent budget burns at 14.4x: the whole 30-day budget in about two days
    assert 0.144 / s.budget == pytest.approx(14.4)
    assert slo.burn_expr(s, "1h") == "slo:error_ratio:x:1h / 0.01"
    assert slo.SLO("y", "d", 0.90, "g", "t").budget == pytest.approx(0.1)


def test_every_slo_gets_a_page_and_a_ticket_alert_over_two_windows_each():
    rules = slo.build_rules(SLOS, slo.STANDARD)
    alerts = {r["alert"]: r for r in rules["groups"][1]["rules"]}
    for s in SLOS:
        page, ticket = alerts[f"SLOBurn_{s.name}_page"], alerts[f"SLOBurn_{s.name}_ticket"]
        assert f"{s.name}:1h" in page["expr"] and f"{s.name}:5m" in page["expr"] and "> 14.4" in page["expr"]
        assert f"{s.name}:6h" in ticket["expr"] and f"{s.name}:30m" in ticket["expr"] and "> 6" in ticket["expr"]
        assert page["expr"].count(" and ") == 1                       # both windows must agree: fast to fire, fast to clear
        assert page["labels"]["severity"] == "page" and ticket["labels"]["severity"] == "ticket"


def test_lab_windows_are_minutes_but_use_the_same_thresholds():
    standard, lab = slo.build_rules(SLOS, slo.STANDARD), slo.build_rules(SLOS, slo.LAB)
    text = yaml.safe_dump(lab)
    assert ":1m" in text and ":5m" in text and ":1h" not in text and ":6h" not in text
    a = next(r for r in lab["groups"][1]["rules"] if r["alert"] == "SLOBurn_ticket_availability_page")
    b = next(r for r in standard["groups"][1]["rules"] if r["alert"] == "SLOBurn_ticket_availability_page")
    assert "> 14.4" in a["expr"] and "> 14.4" in b["expr"]


def test_recording_rules_exist_for_every_window_an_alert_uses():
    rules = slo.build_rules(SLOS, slo.STANDARD)
    recorded = {r["record"] for r in rules["groups"][0]["rules"]}
    for r in rules["groups"][1]["rules"]:
        if r["alert"].startswith("SLOBurn_"):
            for name in [tok for tok in r["expr"].replace("(", " ").split() if tok.startswith("slo:error_ratio:")]:
                assert name in recorded, name


def test_a_quiet_period_cannot_page(tmp_path):
    rec = slo.build_rules(SLOS, slo.LAB)["groups"][0]["rules"][0]
    assert "> 0.02" in rec["expr"]                                 # the traffic floor is part of the recorded ratio


def test_non_slo_alerts_cover_leaks_drift_breakers_and_the_degraded_tier():
    names = {r["alert"] for r in slo.build_rules(SLOS)["groups"][1]["rules"]}
    assert {"RestrictedFactLeaked", "DriftCritical", "CircuitBreakerOpen", "DegradedTierServing"} <= names


def test_the_committed_rule_files_are_what_the_generator_produces():
    out = ROOT / "observability" / "prometheus" / "rules"
    assert (out / "slo.rules.yml").read_text(encoding="utf-8") == slo.dump(slo.build_rules(SLOS, slo.STANDARD))
    assert (out / "slo.lab.rules.yml").read_text(encoding="utf-8") == slo.dump(slo.build_rules(SLOS, slo.LAB))


@pytest.mark.skipif(not Path(PODMAN).exists(), reason="no container runtime to run promtool in")
@pytest.mark.parametrize("name", ["slo.rules.yml", "slo.lab.rules.yml"])
def test_prometheus_accepts_the_generated_rules(name):
    rules = ROOT / "observability" / "prometheus" / "rules" / name
    p = subprocess.run([PODMAN, "run", "--rm", "-v", f"{rules}:/rules.yml:ro", "--entrypoint", "promtool", "docker.io/prom/prometheus:latest",
                        "check", "rules", "/rules.yml"], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stdout + p.stderr
