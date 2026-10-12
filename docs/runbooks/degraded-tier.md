# The degraded tier is answering (`DegradedTierServing`, `SLOBurn_no_degraded_tier_*`)

**What it means.** The local fallback model is serving requests because the cloud routes failed or were skipped by their circuit breakers. Failover keeps the system up, not the answers good: in the system design lab the
local 1B model dropped category accuracy from 7/7 to 4/7 and ignored the refusal rule.

**Who is affected.** Tickets served by it are HELD (never released); knowledge answers are held or labelled by policy. So customers do not get wrong replies, but the review queue fills and cost per request FALLS to zero,
which looks like good news on a cost chart. It is not.

## The first five minutes

```bash
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status     # which breakers are open, which routes failed
# Prometheus: sum by (route, tier) (rate(ai_gateway_requests_total[5m]))
# Prometheus: sum by (route, reason) (increase(ai_gateway_attempt_failures_total[10m]))
```

## Likely causes and what to do

- **Both cloud routes failing.** Provider trouble or quota: [provider-trouble](provider-trouble.md), [quota](quota.md). Nothing to do for the held tickets except fix the cause; they are not lost.
- **Breakers open after a short blip.** A breaker resets on its own after its timeout (30 s by default); a successful probe closes it. If it stays open, the provider is still failing.
- **The quota guard is on and shedding.** Tickets are refused cleanly instead of falling back; expected, see [quota](quota.md).

Do not "fix" this by releasing degraded answers. If the fallback must answer for a particular use, that is a policy decision (label, not hold), made in advance and recorded.

## Escalate when

the cause is not the provider and not the quota.

## Afterwards

Check that no degraded answer was released (`ai_knowledge_answers_total{degraded="true"}` against its action), and that the held tickets were reviewed or re-run.
