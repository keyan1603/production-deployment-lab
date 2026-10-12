# Incident process, and the kill switch

Applies to every alert. The goal of the first minutes is to stop the harm and keep evidence, not to find the root cause.

## The first five minutes

1. **Say you have it.** One person owns the incident; everyone else supports.
2. **Look at what is serving.** Which release is active, is a canary running, what changed recently?
   ```bash
   curl -s -H "X-Admin-Key: $ADMIN_KEY" $TICKETS/admin/releases
   curl -s -H "X-Admin-Key: $ADMIN_KEY" $KNOWLEDGE/admin/releases
   ```
3. **If a release changed in the last hour, roll it back first and ask questions later.** A rollback is one call and takes about a fifth of a second.
   ```bash
   curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"reason":"incident, rolling back"}' $TICKETS/admin/releases/rollback
   curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"reason":"incident, rolling back"}' $KNOWLEDGE/admin/releases/rollback
   ```
4. **Look at the dependencies.** Readiness of each service, and the gateway's routes and quota.
   ```bash
   curl -s $TICKETS/readyz; curl -s $KNOWLEDGE/readyz; curl -s $GATEWAY/readyz
   curl -s -H "X-Service: monitor-agent" -H "X-Service-Key: $MONITOR_KEY" $GATEWAY/v1/status
   ```
5. **Find the request.** Every response has an `X-Correlation-Id`; every log line and every trace carries it. Search the logs for it, then open the trace by the `trace_id` on the same line.

## The kill switch

If tickets are being processed wrongly and a rollback does not fix it, stop accepting new tickets. Work already in progress finishes; callers get a `503` with a `Retry-After`, which is
better than a wrong reply or a held ticket nobody can explain.

```bash
curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"on": true, "reason": "wrong replies, investigating"}' $TICKETS/admin/freeze
curl -s -H "X-Admin-Key: $ADMIN_KEY" $TICKETS/admin/freeze          # check the state
curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"on": false}' $TICKETS/admin/freeze
```

`TicketsFrozen` is an alert on purpose: a freeze that is forgotten is an outage.

## Escalate when

- a restricted fact has appeared in an answer (see [restricted-fact-leaked](restricted-fact-leaked.md)): immediately, whatever the hour;
- you cannot stop the harm within fifteen minutes;
- you do not understand what you are looking at.

## Afterwards

Write down the timeline from the alert times and the release events (`/admin/releases` keeps the last twenty), what stopped the harm, what would have caught it sooner, and one change to the gate,
the alerts or this page. The change is the point of the exercise.
