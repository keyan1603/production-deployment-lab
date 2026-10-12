# Quota (`QuotaNearlyExhausted`, `QuotaGuardShedding`)

**What it means.** A model has used most of its daily allowance (`QuotaNearlyExhausted`, past 80 percent), or the gateway is already refusing the lowest-priority callers to protect what is left (`QuotaGuardShedding`).
The gateway counts every request that reaches the provider, including failed ones, per model per provider day, and keeps the count across restarts.

The thresholds are per caller. In the lab: tickets are refused at 85 to 90 percent, the knowledge assistant at 98 percent, and the monitor's probes are never refused before the allowance is gone, so there is still a
signal about answer quality at the end of the day.

**Who is affected.** Tickets are held with a `model_unavailable` reason (fast, instead of a slow failure through the failover chain). Knowledge answers continue until their threshold.

## The first five minutes

```bash
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status      # quota.<model>.used / limit / ratio
# Prometheus: max by (model) (ai_gateway_quota_used);  sum by (caller, reason) (increase(ai_gateway_shed_total[1h]))
```

## What to do

1. **Is it the usual curve or a spike?** Tickets per hour times model calls per ticket (about four) is the expected spend. A jump in calls per ticket points at a release (longer tool loops) or retries.
2. **Capacity arithmetic.** Free tier: 500 requests a day per model is about 100 tickets a day, whatever the number of service instances. More instances do not add allowance; they add contention.
3. **Make a choice, in this order:** wait for the reset (the 429 body says how long), move to a paid or second key (a decision for the owner, not for the on-call), or hold new tickets with the freeze switch
   (`POST $TICKETS/admin/freeze`) so the remaining allowance goes to the knowledge assistant.
4. **Do not raise the shed thresholds in the middle of an incident.** They are the priority order.

## Escalate when

the allowance is spent early in the provider's day with no release or traffic change to explain it.

## Afterwards

Check `QUOTA_RPD` and `QUOTA_RESET_UTC_HOUR` still match the provider's published limits and its actual reset time; both are configuration, not facts.
