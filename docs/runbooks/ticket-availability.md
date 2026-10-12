# Ticket API errors (`SLOBurn_ticket_availability_*`)

**What it means.** The ticket API is answering with server errors (5xx) faster than the budget allows (99 percent good). Note: a held ticket is a 200, not a 5xx; look at
[tickets-held](tickets-held.md) for those.

**Who is affected.** Anyone submitting a ticket. An error is worse than a held ticket: the caller has no outcome at all.

## The first five minutes

```bash
curl -s $TICKETS/healthz        # is the process alive?
curl -s $TICKETS/readyz         # which dependency is failing?
# Prometheus: sum by (status_class) (rate(http_server_requests_total{service_name="tickets",path="/tickets"}[5m]))
# Prometheus: sum(rate(ai_ticket_outcomes_total{action="error"}[5m]))
```

## Likely causes and what to do

- **`/healthz` fails or the process is restarting.** The service crashed. Read its log (JSON lines; the last `"level": "ERROR"` has the trace and the correlation id) and restart it. If it crashed after a
  release was activated, roll that release back.
- **`/readyz` shows `gateway` or `identity` unavailable.** A hard dependency is down; tickets cannot be processed. Restore that service first. The ticket service recovers on its own once it answers.
- **Errors only on one release.** `ai_ticket_outcomes_total{action="error"}` split by `release`: roll that release back.
- **Backpressure.** A burst of `429` from the service is the concurrency limit working. If it is sustained, the cause is usually the provider, not the service; see [provider-trouble](provider-trouble.md). Adding ticket
  instances does not add throughput when the provider's quota is the limit (see the capacity results in the README).

## Escalate when

the process will not stay up after a rollback, or the errors are on the gateway or identity service and a restart does not clear them.

## Afterwards

Add the failing case to the offline tests so it cannot return unseen.
