# A restricted fact appeared in an answer (`RestrictedFactLeaked`)

**What it means.** A synthetic probe asking as a user who may NOT read a document got an answer containing that document's canary fact. The objective is zero and no tolerance applies. A probe is a real request to the
real service, so a real user could have received the same answer.

**Who is affected.** Whoever asked questions of the knowledge assistant in the window since the fault began. Treat it as a data incident until shown otherwise.

## The first five minutes

1. **Stop new answers.** Freeze ticket intake if tickets can reach the knowledge service, and take the knowledge release back to the last one known good.
   ```bash
   curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"on": true, "reason": "restricted fact leaked"}' $TICKETS/admin/freeze
   curl -s -X POST -H "X-Admin-Key: $ADMIN_KEY" -d '{"reason":"leak"}' $KNOWLEDGE/admin/releases/rollback
   ```
2. **Keep the evidence.** The monitor's `/state` lists the failing probe; the knowledge service's audit file records, for every answer, the caller, the groups the token carried, the documents retrieved and the documents cited.
   ```bash
   curl -s -H "X-Admin-Key: $ADMIN_KEY" $MONITOR/state
   ```
3. **Escalate now**, whatever the hour.

## Likely causes

- the access filter was dropped or bypassed in a change (retrieval returned documents the caller's groups do not allow);
- a cache served one user's answer to another (check the gateway cache scope: only the knowledge service may use the cache, and its scope carries the user's access level);
- a token carried more groups than the user holds (check the identity service's exchange and the service ceilings).

## Escalate when

Immediately, whatever the hour, as step 3 says. Do not wait to confirm a second leak.

## Afterwards

The answer to "how many real users saw it?" comes from the audit records, not from guessing. Write that down before the incident is closed.
