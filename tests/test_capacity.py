"""The capacity arithmetic, and a check that it agrees with the numbers the capacity study measured (the log is evidence in the blog folder)."""

import pytest

from deploylab import capacity as cap


def test_quota_caps_throughput():
    assert cap.max_tickets_per_minute(60, 4) == 15
    assert cap.max_tickets_per_minute(15, 4) == pytest.approx(3.75)
    assert cap.max_tickets_per_day(500, 4) == 125
    assert cap.max_tickets_per_day(500, 4, reserve=0.15) == pytest.approx(106.25)       # what a guard that sheds tickets at 85 percent leaves for them


def test_littles_law_for_a_closed_loop():
    assert cap.mean_time_per_ticket(1, 12) == pytest.approx(5.0)
    assert cap.mean_time_per_ticket(16, 15) == pytest.approx(64.0)
    assert cap.clients_for_target(15, 60) == 15
    assert cap.clients_for_target(15, 1) == 1                          # never below one


def test_it_matches_the_study_measurements():
    """Sweep of closed-loop clients against one ticket service with a 60-request-a-minute quota (phase4_capacity.log, E1)."""
    measured = {1: (11.2, 5.17), 2: (14.8, 7.04), 4: (16.1, 14.44), 8: (14.5, 27.70), 16: (14.7, 52.04)}      # clients: (tickets/min, p50 seconds)
    ceiling = cap.max_tickets_per_minute(60, 4)
    for clients, (throughput, p50) in measured.items():
        assert throughput <= ceiling * 1.10                              # never meaningfully above the quota-bound ceiling
    for clients in (8, 16):                                              # saturated: the average time is what Little's law says
        throughput, p50 = measured[clients]
        assert cap.mean_time_per_ticket(clients, throughput) == pytest.approx(clients / throughput * 60)
        assert 0.7 * p50 < cap.mean_time_per_ticket(clients, throughput) < 1.6 * p50       # the median sits below the mean in a skewed distribution


def test_quota_needed_counts_the_probes_and_the_headroom():
    probes = cap.probe_budget_per_day(60)
    assert probes == pytest.approx(1200)
    assert cap.quota_needed(100, 4, headroom=0.25, background_calls_per_day=probes) == pytest.approx(2000)
    assert cap.probe_budget_per_day(10) == pytest.approx(7200)           # the interval is a cost setting


def test_independent_limiters_add_up_past_the_providers_limit():
    assert cap.effective_limit_with_instances(60, 1, 60) == 0            # one gateway: exactly at the limit, no excess
    assert cap.effective_limit_with_instances(60, 2, 60) == 60           # two: the provider can be asked for twice what it allows
