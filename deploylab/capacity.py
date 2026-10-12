"""Capacity arithmetic for a system whose bottleneck is someone else's rate limit.

Where the limit is the model provider's quota, three facts decide everything, and each is a line of arithmetic:

    1. Throughput is capped by quota, not by servers:
           tickets per minute  <=  provider requests per minute / model calls per ticket
           tickets per day     <=  provider requests per day    / model calls per ticket
    2. Once that cap is reached, more concurrent clients only wait. With C clients in a closed loop and throughput X,
       Little's law gives the average time per ticket:   W = C / X
    3. More service instances add contention, not throughput: they share the same quota. More GATEWAY instances are worse,
       because each enforces its own limiter against the one provider quota.

The functions are pure so a test can check them against what the capacity study measured.
"""

from __future__ import annotations

import math


def max_tickets_per_minute(provider_rpm: float, calls_per_ticket: float) -> float:
    return provider_rpm / calls_per_ticket


def max_tickets_per_day(provider_rpd: float, calls_per_ticket: float, reserve: float = 0.0) -> float:
    """`reserve` is the share of the daily allowance kept for higher-priority callers (the quota guard's shed threshold is 1 - reserve)."""
    return provider_rpd * (1.0 - reserve) / calls_per_ticket


def mean_time_per_ticket(clients: int, throughput_per_min: float) -> float:
    """Little's law for a closed loop: the average time a ticket spends in the system, in seconds, when `clients` always have one outstanding."""
    return clients / (throughput_per_min / 60.0)


def clients_for_target(throughput_per_min: float, target_seconds: float) -> int:
    """The most closed-loop clients that keep the average time per ticket under `target_seconds` at this throughput."""
    return max(1, math.floor(target_seconds * throughput_per_min / 60.0))


def quota_needed(tickets_per_day: float, calls_per_ticket: float, headroom: float = 0.2, background_calls_per_day: float = 0.0) -> float:
    """Provider requests per day to buy or negotiate for a target volume, with headroom and the monitor's own probes counted."""
    return (tickets_per_day * calls_per_ticket + background_calls_per_day) * (1.0 + headroom)


def probe_budget_per_day(interval_s: float) -> float:
    """Model calls per day the synthetic probes spend: most probe kinds make one call, the injection probe makes none (5 of 6)."""
    return 86400.0 / interval_s * 5.0 / 6.0


def effective_limit_with_instances(provider_limit: float, gateway_instances: int, per_instance_limit: float) -> float:
    """What the provider sees from independent limiters: they add up, so the provider's limit is exceeded as soon as the sum is."""
    return gateway_instances * per_instance_limit - provider_limit
