# Runbooks

One page per alert family. Every alert in `observability/prometheus/rules/` carries a `runbook` annotation pointing at one of these pages
(a test fails if one is missing or if a page names an endpoint the services do not have).

Each page has the same shape: **what it means**, **who is affected**, **the first five minutes** (commands you can paste), **likely causes and what to do**,
**when to escalate**, **afterwards**.

| Alert | Page |
|---|---|
| `SLOBurn_ticket_availability_*` | [ticket-availability](ticket-availability.md) |
| `SLOBurn_ticket_safe_completion_*` | [tickets-held](tickets-held.md) |
| `SLOBurn_ticket_latency_*` | [ticket-latency](ticket-latency.md) |
| `SLOBurn_answer_quality_probe_*`, `DriftCritical` | [answer-quality](answer-quality.md) |
| `SLOBurn_no_degraded_tier_*`, `DegradedTierServing` | [degraded-tier](degraded-tier.md) |
| `RestrictedFactLeaked` | [restricted-fact-leaked](restricted-fact-leaked.md) |
| `CircuitBreakerOpen` | [provider-trouble](provider-trouble.md) |
| `QuotaNearlyExhausted`, `QuotaGuardShedding` | [quota](quota.md) |
| `DailyBudgetNearlyExhausted`, `BudgetShedding`, `CostPerTicketHigh` | [cost](cost.md) |
| `TicketsFrozen` | [incident-process](incident-process.md) |

## Variables used in the commands

```bash
export TICKETS=http://127.0.0.1:8104 KNOWLEDGE=http://127.0.0.1:8103 GATEWAY=http://127.0.0.1:8102 MONITOR=http://127.0.0.1:8105
export ADMIN_KEY=...        # the X-Admin-Key of the ticket, knowledge and monitor services
export MONITOR_KEY=...      # the monitor-agent service key, for the gateway's /v1/status
```
