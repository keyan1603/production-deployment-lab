# Tickets are being held (`SLOBurn_ticket_safe_completion_*`)

**What it means.** More than the allowed share of tickets end `held` (waiting for a person) or in an error instead of being released or escalated on purpose. The objective is 90 percent.
Note that a 90 percent objective can raise the ticket-level alert but never the page: its largest possible burn rate is 10, below the 14.4 page threshold.

**Who is affected.** Customers whose tickets sit in the review queue. Nothing wrong is sent to them; that is why a held ticket is the safe failure.

## The first five minutes

```bash
# which reasons, by count?  (Prometheus)
#   sum by (reason) (increase(ai_ticket_holds_total[10m]))
# which release is serving, and is a canary running?
curl -s -H "X-Admin-Key: $ADMIN_KEY" $TICKETS/admin/releases
# is the gateway healthy, are routes open, how much quota is used?
curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status
```

## Likely causes and what to do

| Hold reason | Cause | Action |
|---|---|---|
| `triage_invalid` | a prompt or model change broke the output format the code validates | roll the ticket release back: `POST $TICKETS/admin/releases/rollback` |
| `model_unavailable` | every model route failed, or the quota guard is refusing | see [provider-trouble](provider-trouble.md) and [quota](quota.md) |
| `budget_exhausted` | the daily spend cap or a tenant budget was reached | see [cost](cost.md) |
| `capability_unavailable` | the knowledge service is down or refused the operator | check `$KNOWLEDGE/readyz`; see [answer-quality](answer-quality.md) |
| `pipeline_error` | a bug or a dependency error | rollback if a release changed; otherwise escalate with the correlation id |
| degraded-tier answers | the local fallback model served the request | see [degraded-tier](degraded-tier.md) |

## Escalate when

the reason is `pipeline_error` and a rollback does not change it, or held tickets are climbing and you cannot name the reason.

## Afterwards

Was the release gated? If not, why not? If the gate passed it, which evaluation case should have caught it, and is it in the set now?
