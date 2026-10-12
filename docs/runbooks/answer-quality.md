# Answers are getting worse (`SLOBurn_answer_quality_probe_*`, `DriftCritical`)

**What it means.** The synthetic probes, which ask six questions with known right answers every few seconds to minutes, are failing more than the budget allows, or the drift verdict against the saved baseline reads
critical. The probes are judged by the same judge the release gate uses.

**Who is affected.** Everyone using the knowledge assistant, and every ticket that asks it a policy question. Wrong answers are worse than held ones.

## The first five minutes

```bash
curl -s -H "X-Admin-Key: $ADMIN_KEY" $MONITOR/state          # verdict, findings, and the last probe results case by case
curl -s -H "X-Admin-Key: $ADMIN_KEY" $KNOWLEDGE/admin/releases
```

Read `findings` and `recent`. Which kind of probe is failing tells you the cause:

| Failing probes | Likely cause | Action |
|---|---|---|
| the restricted ones answered, or `leaks` above zero | access control failed | STOP: [restricted-fact-leaked](restricted-fact-leaked.md) |
| the "no answer" and restricted ones come back as something other than "I can't find that" | a prompt that lets the model improvise (the "helpful answerer" failure) | roll the knowledge release back: `POST $KNOWLEDGE/admin/releases/rollback` |
| the answerable ones fail | retrieval is broken (embeddings, index), or a model swap | check `$KNOWLEDGE/readyz` (the index check); `degraded_share` in the findings tells you if the fallback is answering |
| all of them error | the knowledge service or the gateway is down | `$KNOWLEDGE/readyz`, [provider-trouble](provider-trouble.md) |

## Escalate when

a rollback does not move the probe results back within two probe intervals.

## Afterwards

If the cause was a release, add the probe or evaluation case that would have caught it to the gate's set. If the baseline looks wrong, change it only with `scripts/save_baseline.py --yes`, by a person, after reading the
metrics; never to silence an alert.
