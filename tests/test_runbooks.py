"""The runbooks must stay true: every alert points at a page, every page's commands name endpoints that exist, and every metric
a page tells the on-call to query is one the services emit. A runbook that sends someone to a URL that no longer exists is worse
than none, and it only fails at 3 a.m. unless a test fails first."""

import re
from pathlib import Path

import yaml

from tests.test_observability_contract import emitted, metrics_in

ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS = ROOT / "docs" / "runbooks"
RULES = [ROOT / "observability" / "prometheus" / "rules" / n for n in ("slo.rules.yml", "slo.lab.rules.yml")]


def alerts():
    for path in RULES:
        for group in yaml.safe_load(path.read_text(encoding="utf-8"))["groups"]:
            for rule in group["rules"]:
                if "alert" in rule:
                    yield rule


def test_every_alert_has_a_runbook_that_exists_and_names_it():
    seen = 0
    for rule in alerts():
        seen += 1
        page = ROOT / rule["annotations"]["runbook"]
        assert page.exists(), (rule["alert"], rule["annotations"]["runbook"])
        text = page.read_text(encoding="utf-8")
        needle = rule["alert"].rsplit("_", 1)[0] if rule["alert"].startswith("SLOBurn_") else rule["alert"]
        assert needle in text, f"{page.name} does not mention {needle}"
    assert seen >= 20


def test_every_page_has_the_same_sections():
    for page in RUNBOOKS.glob("*.md"):
        if page.name in ("README.md", "incident-process.md"):
            continue
        text = page.read_text(encoding="utf-8")
        for heading in ("**What it means.**", "**Who is affected.**", "## The first five minutes", "## Escalate when", "## Afterwards"):
            assert heading in text, (page.name, heading)


def served_paths() -> set:
    paths = {"/healthz", "/readyz"}
    for source in (ROOT / "services").glob("*.py"):
        paths |= set(re.findall(r'Route\("(/[^"]+)"', source.read_text(encoding="utf-8")))
    releases = (ROOT / "deploylab" / "releases.py").read_text(encoding="utf-8")
    prefix = re.search(r'prefix: str = "([^"]+)"', releases).group(1)
    paths |= {prefix} | {prefix + sub for sub in re.findall(r'Route\(f"\{prefix\}(/[a-z]+)"', releases)}
    return paths


def test_every_endpoint_a_runbook_calls_exists_in_the_services():
    served, used = served_paths(), set()
    for page in RUNBOOKS.glob("*.md"):
        for _, path in re.findall(r"\$(TICKETS|KNOWLEDGE|GATEWAY|MONITOR)(/[A-Za-z0-9_/\-]*)", page.read_text(encoding="utf-8")):
            used.add(path)
            assert path in served, f"{page.name} calls {path}, which no service serves"
    assert {"/admin/releases/rollback", "/admin/freeze", "/v1/status", "/state", "/readyz"} <= used


def test_every_metric_a_runbook_mentions_is_emitted():
    known = emitted()
    for page in RUNBOOKS.glob("*.md"):
        unknown = metrics_in(page.read_text(encoding="utf-8")) - known
        assert not unknown, (page.name, sorted(unknown))


def test_the_commands_use_the_variables_the_index_defines():
    index = (RUNBOOKS / "README.md").read_text(encoding="utf-8")
    for page in RUNBOOKS.glob("*.md"):
        for var in set(re.findall(r"\$([A-Z_]+)", page.read_text(encoding="utf-8"))):
            assert f"{var}=" in index, (page.name, var)
