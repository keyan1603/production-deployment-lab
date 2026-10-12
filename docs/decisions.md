# Operations decisions

Short records, each tied to something measured in this repo. Format: context, decision, evidence, consequences.

## O1. One gateway instance per provider API key
- **Context.** The gateway holds the rate limiter, the circuit breakers, the cache, the quota counters and the spend counters, all in process memory.
- **Decision.** Run one. For availability use active-standby, not two active.
- **Evidence.** Two active gateways against one provider quota: 12.9 tickets a minute against 15.9, 17 of 36 tickets held against 4, 95th percentile 140 s against 60 s.
- **Consequences.** A shared store for the counters is the prerequisite for scaling the gateway out; it is not built.

## O2. Scale the ticket service for latency and availability, never for throughput
- **Decision.** Add instances when in-flight tickets or backpressure say the service is the queue, not when more tickets are offered.
- **Evidence.** 1, 2 and 3 instances: 15.7, 14.3 and 16.2 tickets a minute; the provider quota is the ceiling.

## O3. Shed by priority before the provider's hard limit
- **Decision.** The quota guard refuses callers at per-caller thresholds (tickets 85 to 90 percent, the knowledge assistant 98, the monitor never before the allowance is gone). A refused ticket is held with a reason.
- **Evidence.** Allowance of 140 requests, no guard: held tickets took 43 s on average and the provider refused 5 requests. With the guard: 3.8 s, no refusals, 15 percent kept in reserve.
- **Consequences.** Fewer tickets are released in the run. Counts persist across restarts, so a restarted gateway does not hand back the day's allowance (a drill that reset the state in the wrong order showed this: a restart shed
  everything because the previous process had written its counts back).

## O4. A daily spend cap, with the monitor exempt
- **Decision.** When the day's recorded spend reaches the cap, every caller except the exempt ones gets a budget refusal; tickets are held as `budget_exhausted`.
- **Why the monitor is exempt.** The last thing to lose is the signal about answer quality.

## O5. Alerts look back over signals that flap
- **Context.** A circuit breaker re-closes to half-open every 30 s to test its route; a drift verdict is computed over a sliding window.
- **Evidence.** In a 429 storm, `CircuitBreakerOpen` ("open for a minute") never fired in seven minutes. The first drift alert paged and resolved four times during one outage.
- **Decision.** `CircuitBreakerOpen` and `DriftCritical` use `max_over_time` over a look-back (2 and 3 minutes) instead of the instantaneous value.
- **Consequences.** They clear later: `DriftCritical` cleared 334 s after the fix. That is the cost of not paging in a loop.

## O6. The kill switch is itself an alert
- **Decision.** `POST /admin/freeze` stops new tickets and raises `TicketsFrozen`. A freeze nobody remembers is an outage.

## O7. Load tests and drills run against a provider simulator
- **Context.** The free tier allows 500 requests a day per model. One full load sweep would spend it in a minute, and the real provider cannot be made to fail on demand.
- **Decision.** `sim/gemini_sim.py` stands in for the API behind `GOOGLE_GEMINI_BASE_URL`: per-model minute and day quotas in the real error shape, log-normal latency, and switchable faults. Everything else in the drills is real.
- **Consequences.** Results are labelled as simulated and say nothing about answer quality. A simulator whose heavy model has generous limits quietly makes scaling look free, so the heavy model gets the real tier's small allowance.
