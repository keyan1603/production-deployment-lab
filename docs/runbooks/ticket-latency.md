# Tickets are slow (`SLOBurn_ticket_latency_*`)

**What it means.** More than 5 percent of tickets take longer than 60 seconds. A ticket normally makes four model calls, so its time is mostly the provider's latency and the rate limiter's queue.

**Who is affected.** Operators waiting on a reply. Slow tickets also hold concurrency slots, which can turn into `429` backpressure for others.

## The first five minutes

```bash
# Prometheus:
#   histogram_quantile(0.95, sum by (le) (rate(ai_ticket_duration_seconds_bucket[5m])))
#   histogram_quantile(0.95, sum by (le, route) (rate(ai_gateway_duration_seconds_bucket[5m])))   # which route is slow
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status         # breakers, fallbacks, p50
```

## Likely causes and what to do

- **One model route is slow.** The gateway times it out (30 s for the light model, 45 s for the heavy one) and fails over, which can be SLOWER than waiting (measured in the system design lab: 9.7 s with no
  timeout, 16 to 17 s with a 2 s timeout, because the fallback was slower). Do not shorten timeouts in a hurry; compare with the fallback's latency first.
- **The rate limiter is queueing.** Offered load exceeds the provider's per-minute quota. Latency then grows with the queue. Fewer clients, not more instances: see [quota](quota.md).
- **A release made prompts longer.** Compare `ai_gateway_tokens_total` per ticket before and after the release; roll back if it jumped.
- **The local fallback is serving.** It is much slower than the cloud models; see [degraded-tier](degraded-tier.md).

## Escalate when

latency stays high with a healthy provider and no queue: that points at the service itself.

## Afterwards

If a timeout change helped, put the measurement in the decision record before the change is kept.
