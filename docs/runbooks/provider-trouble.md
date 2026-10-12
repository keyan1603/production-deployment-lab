# The model provider is failing (`CircuitBreakerOpen`)

**What it means.** A model route's circuit breaker has been open for a minute: that route failed several times in a row and the gateway is skipping it. Requests fail over to the next route (light, then heavy, then the
local fallback), which changes cost, latency and, at the end of the chain, answer quality.

**Who is affected.** Depends on the chain position: a light route open means more cost and latency; all cloud routes open means held tickets ([degraded-tier](degraded-tier.md)).

## The first five minutes

```bash
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status
# Prometheus: sum by (route, reason) (increase(ai_gateway_attempt_failures_total[10m]))
```

Read the attempt errors in the gateway log (the JSON lines carry them): the error text says whether it is `429 RESOURCE_EXHAUSTED` (quota), `500/503` (provider), a timeout, or a connection error.

## Likely causes and what to do

- **`429` with a per-minute quota.** The rate limiter should keep the gateway under it. If it does not, there is a second gateway instance or another client on the same key: each limiter only sees its own traffic.
  Run ONE gateway per API key.
- **`429` with a per-day quota.** The allowance is spent. Nothing to do but wait for the reset or use another key; the quota guard exists to make this a clean refusal. See [quota](quota.md).
- **`500` / `503`.** The provider is having trouble. The breaker reopens a probe after its timeout; the system recovers by itself when the provider does. Check the provider's status page; do not restart our services.
- **Timeouts.** The route is slow; see [ticket-latency](ticket-latency.md).
- **Auth errors (`401`/`403`).** The key was rotated or revoked. Replace the secret file the gateway reads (`GEMINI_API_KEY_FILE`) and restart the gateway.

## Escalate when

the provider is healthy by its own status and the breaker will not close.

## Afterwards

Did `preflight` cover the fallback routes? A route that has never been exercised is a hope.
