# Capacity and scaling

The question for a system like this is not "how many servers" but "how much of someone else's quota does a ticket use, and who is allowed to spend it". These notes give the arithmetic
(`deploylab/capacity.py`, tested against the measurements) and the scaling policy that follows from it. Measurements are from the capacity study (`scripts/phase4_capacity.py`,
`docs/results/phase4_capacity.log`) against the provider simulator, with the provider's quota set to 60 requests a minute and about four model calls per ticket. They show how the system behaves
against a rate-limited provider; they say nothing about model quality.

## The arithmetic

| Quantity | Formula | This system |
|---|---|---|
| Ceiling on tickets per minute | provider requests per minute / calls per ticket | 60 / 4 = 15 |
| Ceiling on tickets per day | provider requests per day / calls per ticket | free tier: 500 / 4 = 125, or about 106 if tickets are shed at 85 percent |
| Average time per ticket, saturated, C clients in a closed loop | C / throughput (Little's law) | 16 clients at 14.7 a minute: 65 s (measured median 52 s, 95th percentile 98 s) |
| Quota to ask for | (tickets per day x calls per ticket + probe calls) x (1 + headroom) | 100 tickets a day with 25 percent headroom and a 60 s probe interval: 2,000 requests a day |

Probe calls are part of the bill: one probe every 60 seconds is about 1,200 calls a day, more than twice the free tier's whole daily allowance of 500. The probe interval is a cost setting.

## What the measurements say

1. **Throughput stops growing at the quota, with two clients.** From 2 clients up, the system answers 14.5 to 16.1 tickets a minute whatever the load. What changes is the wait: the median ticket takes 5 s with
   one client, 28 s with eight and 52 s with sixteen, and the 95th percentile reaches 98 s.
2. **More ticket-service instances add nothing.** One, two and three instances, same 12 clients: 15.7, 14.3 and 16.2 tickets a minute, medians of 41, 46 and 41 s. All of them share one quota.
3. **More gateway instances make it worse.** Each gateway enforces its own rate limiter and cannot see the other's traffic, so two of them together can ask the provider for twice what it allows.
   One gateway: 15.9 tickets a minute, 4 of 36 held, 7 provider refusals. Two gateways: 12.9 a minute, **17 of 36 held**, 12 refusals, 95th percentile 140 s (calls fell through to the fallback and the held queue).
   A first run in which the simulated heavy model had the light model's generous limits showed two gateways as twice as fast (30 tickets a minute): the extra throughput was calls spilling onto the expensive tier
   and the fallback, which a real free-tier heavy model (20 a day) could not have absorbed. It is kept in the blog folder because the mistake is easy to make.
4. **A guard changes what failure looks like, not how much work fits.** With a 140-request allowance running out mid-run, no guard: 35 tickets released, 13 held after an average of 43 s each, 5 provider refusals.
   With the guard shedding tickets at 85 percent: 29 released, 19 held after an average of **3.8 s** each, no provider refusals, 15 percent of the allowance still available to higher-priority callers. The guard
   releases fewer tickets in the run (29 against 35); what it buys is a fast, explained refusal, no failover storm, and a reserve.

## Scaling policy

| Component | Scale on | Do not |
|---|---|---|
| Ticket service | in-flight tickets and `429` backpressure, to improve latency and availability | expect throughput; it is bounded by the provider quota |
| Knowledge service | in-flight requests | scale before the quota is understood; each request costs the same allowance |
| Gateway | **one instance per provider API key** | add instances: the limiter, the breakers, the quota counters and the spend counters are all per process |
| Monitor | nothing | run more than one: the probes spend quota |

High availability for the gateway is an active-standby pair, not two active instances, until the limiter and the counters live in a shared store (Redis is the open-source choice). That is not built here, and
`deploylab/guards.py` says so.

## Buying capacity

The levers, in the order they are usually worth pulling: lower the calls per ticket (a shorter tool loop; the gate warns when tokens per ticket rise 25 percent), cache what is safe to cache (only the knowledge
service; see the cache policy in the README), raise the quota (a paid tier or a second key is a decision for the owner, not for the on-call), and only then add compute.
