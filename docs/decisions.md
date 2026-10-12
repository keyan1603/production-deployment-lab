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

## O8. Keep serving while draining; the drain must outlast the platform's convergence
- **Context.** On a stop signal the service reports unready, waits, then closes its listener. Kubernetes (like any load balancer) takes a moment to stop sending traffic to a terminating pod.
- **Evidence.** Rolling restarts under load: with new tickets refused during the drain, 2 of 97 and 3 of 123 failed, the one refusal traced was within a second of the stop signal; raising the drain from 5 to 15 s changed nothing, because the refusals were not in the
  tail of the window but at its start. Serving through the drain: 0 of 121.
- **Decision.** `/readyz` flips at once; work is not refused until the listener closes. `DRAIN_SECONDS` is 15 so the probes (period 5 s, threshold 2) and the endpoints have converged before it ends.
- **Consequences.** This reverses Phase 1's behaviour (refuse new tickets while draining). A service with no balancer in front of it is unaffected either way.

## O9. Kubernetes, locally first
- **Decision.** The cloud step is a local Kubernetes cluster (kind on Podman): free, no account, reproducible, and the manifests are the portable part. The pod security standard is enforced by the cluster, the network is default-deny, each pod mounts only
  its own secret files, and the gateway is one replica with a Recreate strategy.
- **Consequences.** A managed cluster changes the registry, the ingress and the secret store, not the manifests' shape. What a hosted platform adds (image pulls, node start-up, a real load balancer) is not measured here.
