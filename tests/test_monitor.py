"""Offline tests for the probe-and-drift monitor, with scripted answers and a scripted gateway status."""

import json

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deploylab import gate, telemetry as tele
from deploylab.monitor import PROBE_IDS, Monitor, VERDICT
from knowledge.corpus import CASES, DOCS
from monitoring.drift import save_baseline

BY_ID = {c.id: c for c in CASES}
CANARY = {d.id: d.canary for d in DOCS}


def good_answer(case) -> str:
    """What a healthy system says for each kind of probe."""
    if case.expect == "answer":
        return f"{' '.join(case.facts)} [{case.doc}]"
    if case.expect == "blocked":
        return gate.WITHHELD
    return gate.NOT_FOUND_PREFIX + "."


def gateway(requests=10, fallbacks=0, cost=0.001, degraded=0):
    return {"routes": [{"name": "gemini-light", "tier": "light"}, {"name": "local-llama", "tier": "degraded"}],
            "audit": {"requests": requests, "fallbacks": fallbacks, "total_cost_usd": cost, "p50_latency_ms_uncached": 900.0,
                      "by_route": {"gemini-light": requests - degraded, "local-llama": degraded}}}


BASELINE = {"correct_rate": 1.0, "authorized_correct_rate": 1.0, "leaks": 0, "fallback_rate": 0.0, "degraded_share": 0.0,
            "cost_per_request_usd": 0.0001, "p50_latency_ms": 900.0}


@pytest.fixture
def tel():
    reader = InMemoryMetricReader()
    t = tele.setup("monitor", "v1", span_exporter=InMemorySpanExporter(), metric_reader=reader, set_global=False)
    previous, tele._current = tele._current, t
    t.reader = reader
    yield t
    tele._current = previous


def make(tmp_path, ask=good_answer, status=None, baseline=BASELINE):
    path = tmp_path / "baseline.json"
    if baseline is not None:
        save_baseline(baseline, path)
    box = {"status": status or gateway()}
    return Monitor(ask, lambda: box["status"], path, min_results=6), box


def value(tel, name, **attrs):
    """Sum of every data point of a metric whose attributes include `attrs` (None if the metric does not exist)."""
    total, found = 0, False
    for rm in tel.reader.get_metrics_data().resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    for p in m.data.data_points:
                        if all(p.attributes.get(k) == v for k, v in attrs.items()):
                            total += p.value if hasattr(p, "value") else p.count
                            found = True
    return total if found else None


def run_ticks(mon, n):
    for _ in range(n):
        mon.tick()


def test_probe_set_covers_every_kind_of_failure_worth_watching():
    kinds = {BY_ID[i].expect for i in PROBE_IDS}
    assert kinds == {"answer", "denied", "unknown", "blocked"}


def test_a_healthy_system_reads_stable_after_one_full_rotation(tmp_path, tel):
    mon, _ = make(tmp_path)
    run_ticks(mon, 6)
    assert mon.state.verdict["status"] == "stable" and mon.correct_ratio() == 1.0
    assert value(tel, "ai.probe.results", case_kind="answer", result="pass") == 2
    assert value(tel, "ai.drift.verdict") == 0 and value(tel, "ai.probe.correct_ratio") == 1.0


def test_probes_rotate_through_every_case(tmp_path, tel):
    mon, _ = make(tmp_path)
    run_ticks(mon, 12)
    assert [r.case_id for r in list(mon.state.results)] == list(PROBE_IDS) * 2


def test_no_verdict_until_there_are_enough_results(tmp_path, tel):
    mon, _ = make(tmp_path)
    run_ticks(mon, 5)
    assert mon.state.verdict["status"] == "no_baseline" and mon.state.metrics == {}


def test_no_baseline_is_reported_not_guessed(tmp_path, tel):
    mon, _ = make(tmp_path, baseline=None)
    run_ticks(mon, 6)
    assert mon.state.verdict["status"] == "no_baseline"


def test_a_restricted_fact_in_an_answer_is_a_critical_leak(tmp_path, tel):
    def leaky(case):
        if case.id == "K02":
            return gate.NOT_FOUND_PREFIX + ", but " + CANARY["ENG-001"]
        return good_answer(case)

    mon, _ = make(tmp_path, ask=leaky)
    run_ticks(mon, 6)
    v = mon.state.verdict
    assert v["status"] == "critical" and any(f["metric"] == "leaks" for f in v["findings"])
    assert value(tel, "ai.probe.leaks") == 1 and value(tel, "ai.drift.verdict") == 2


def test_quality_dropping_below_the_baseline_is_critical(tmp_path, tel):
    def sloppy(case):
        return "I think the answer is probably something like that." if case.expect == "answer" else good_answer(case)

    mon, _ = make(tmp_path, ask=sloppy)
    run_ticks(mon, 6)
    assert mon.state.verdict["status"] == "critical"
    assert {f["metric"] for f in mon.state.verdict["findings"]} >= {"correct_rate", "authorized_correct_rate"}
    assert value(tel, "ai.drift.finding", metric="correct_rate", severity="critical") == 1


def test_answers_served_by_the_degraded_tier_are_critical_even_if_the_answers_look_right(tmp_path, tel):
    mon, box = make(tmp_path)
    run_ticks(mon, 6)
    assert mon.state.verdict["status"] == "stable"
    box["status"] = gateway(requests=30, fallbacks=15, degraded=12, cost=0.001)       # 20 more requests, 12 of them on the local model
    run_ticks(mon, 2)
    v = mon.state.verdict
    assert v["status"] == "critical" and any(f["metric"] == "degraded_share" for f in v["findings"])


def test_a_fallback_rise_alone_is_only_a_warning(tmp_path, tel):
    mon, box = make(tmp_path)
    run_ticks(mon, 6)
    box["status"] = gateway(requests=30, fallbacks=6, degraded=0)
    run_ticks(mon, 1)
    assert mon.state.verdict["status"] == "warn" and value(tel, "ai.drift.verdict") == 1


def test_a_probe_that_cannot_run_counts_as_a_failure_not_a_crash(tmp_path, tel):
    def down(case):
        raise ConnectionError("knowledge service unreachable")

    mon, _ = make(tmp_path, ask=down)
    run_ticks(mon, 6)
    assert all(r.error.startswith("ConnectionError") for r in mon.state.results)
    assert value(tel, "ai.probe.results", result="error") == 6 and mon.state.verdict["status"] == "critical"


def test_the_monitor_never_writes_the_baseline(tmp_path, tel):
    mon, _ = make(tmp_path, baseline=None)
    run_ticks(mon, 12)
    assert not mon.baseline_path.exists()


def test_snapshot_is_json_serialisable(tmp_path, tel):
    mon, _ = make(tmp_path)
    run_ticks(mon, 6)
    assert json.loads(json.dumps(mon.snapshot()))["verdict"] == "stable"
    assert set(VERDICT) == {"stable", "no_baseline", "warn", "critical"}
