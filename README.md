# production-deployment-lab

The second half of the capstone: take the reference system from the [AI System Design lab](https://github.com/keyan1603/ai-system-design-lab) and run it the way a team would run it for real. The system itself (gateway, support-ticket platform, access-controlled knowledge assistant, identity, A2A) is imported from that repo unchanged; this repo adds what only shows up once the pieces are separate, long-running processes: configuration and secrets, health and readiness, graceful shutdown, release safety, observability and operations.

It is built on [Requisite](https://github.com/requisite-ai/requisite-ai), Google's Agent Development Kit and the provider SDKs.

## What runs today

Four services, each its own OS process, started with `deploylab/stack.py`:

| Service | Port | Role |
|---|---|---|
| `identity` | 8101 | login and token exchange (lab-grade tokens, as in the system design lab) |
| `gateway` | 8102 | the AI gateway as a network service; the **only** service holding the model provider key |
| `knowledge` | 8103 | the knowledge assistant as an A2A server; builds its index in the background |
| `tickets` | 8104 | the ticket platform behind `POST /tickets` |

Every call between services keeps its meaning across the wire: a spent budget is still a `CostLimitException`, an exhausted gateway is still a `GatewayExhausted`, and the tenant, operator and correlation id still reach the gateway's audit (`deploylab/remote.py`).

### What makes them deployable

- **Configuration** (`deploylab/config.py`): everything from the environment; secrets from `NAME` or a mounted `NAME_FILE`; a bad configuration refuses to start and lists every problem at once; a secret prints as `***`.
- **Probes** (`deploylab/runtime.py`): `/healthz` is liveness and never looks at dependencies; `/readyz` is readiness and does. Hard dependencies gate readiness; a soft dependency (the ticket service's knowledge service) is reported but does not, so a partial outage is not turned into a total one. Readiness is answered from the last result and refreshed in the background, so a probe never waits on a dead dependency.
- **Graceful shutdown**: on a stop signal the service reports unready, keeps serving for `DRAIN_SECONDS` so a load balancer notices, refuses new work, lets running requests finish for up to `GRACE_SECONDS`, and exits 0.
- **Start-up**: the port opens immediately; slow work (the knowledge index) runs in the background and retries until its dependency is up, so there is no required start order.
- **Back-pressure**: the ticket service runs a bounded number of tickets at once and answers `429` with `Retry-After` instead of queueing without limit.
- **Logs**: one JSON line per event with the correlation id, and bearer tokens, service keys and API keys scrubbed from anything written.
- **Cache policy per caller**: the shared gateway only serves the knowledge service from its semantic cache (its cache scope carries the user's access level); the ticket service, whose answers depend on operator-specific tool results, is never served from or stored in it.

### Containers

`deploy/Dockerfile` and `deploy/compose.yaml` run the same four services as unprivileged (`uid 10001`), read-only containers with every capability dropped, file-mounted secrets and TLS between the services. They are built and run with **Podman** and **podman-compose** (both open source); the files are standard, so `docker build` and `docker compose` work the same. `tests/test_deploy_files.py` also checks that every service's configuration loads from exactly what the compose file declares.

- **TLS between services.** ADK's A2A client refuses to fetch an agent card over plain `http` unless the host is loopback, so a deployment where services have their own addresses needs HTTPS. `scripts/make_secrets.py` creates a private CA and a server certificate (`deploylab/certs.py`, the `cryptography` library); every service serves TLS and trusts only that CA through `SSL_CERT_FILE`.
- **The gateway is the exception.** It is the one service that also calls the public internet, so it gets a bundle of the public roots plus the lab CA. Giving it the lab CA alone broke its calls to the model provider.

## Release safety

What changes an AI system's behaviour without a code change is its prompts, its model pins and its tunable parameters. Here each of those is a **release**: an immutable manifest (`releases/<id>.json`) with a content digest, edited only by creating a new one (a hand-edited manifest fails its digest check).

| Piece | What it does |
|---|---|
| `deploylab/releases.py` | `Release`, the registry, the lint, `ReleaseSet` (active release, canary share, shadow release, per-release statistics, the automatic rollback rule) and the admin API |
| `deploylab/gate.py`, `scripts/release_gate.py` | the gate: lint, model-pin check, a staging copy of the whole system, a black-box evaluation, comparison with the stored baseline |
| ticket and knowledge services | hold several releases at once; every ticket response names the release and digest that served it |
| `.github/workflows/release-gate.yml` | the same gate as a pull-request check (not run yet: the repository is not on GitHub) |

**The gate, cheapest check first.** (1) The manifest is well formed: no missing or unknown prompt name (a typo must not pass silently), no empty or oversized prompt, nothing credential-shaped, the policy prompt still names the tool the resolver is given. (2) Every pinned model exists at the provider, checked with a metadata lookup that generates nothing. (3) A staging copy of all four services starts with the candidate, and the gate checks that the candidate is what answers and that the gateway runs the pinned models. (4) 9 labelled tickets and 13 knowledge questions run through it over HTTP and A2A. (5) The result is compared with the stored baseline using rules fixed before the run: **hard failures** (a restricted fact in an answer, a request that errored, an injection not stopped, the wrong release answering), **regressions** (a drop of two or more cases against the baseline) and **warnings** (a one-case drop, a cost rise).

**Rollout.** `activate`, `canary` (a stable share chosen by hashing the ticket id), `shadow` (a copy of active traffic is run on the candidate after the real answer is sent, and only compared) and `rollback`, each one authenticated call. During a canary the service compares the canary's bad-outcome rate (held or errored) with the control's and removes the canary by itself when it is 30 points worse after at least 6 canary requests.

```bash
venv/Scripts/python scripts/make_releases.py                       # write the five example manifests
venv/Scripts/python -u scripts/release_gate.py r1-baseline --store # evaluate the baseline once and keep its results
venv/Scripts/python -u scripts/release_gate.py r2-shorter-reply    # gate a candidate (exit 0 pass, 1 fail, 2 stopped early)
venv/Scripts/python -u scripts/phase2_study.py                     # live rollout: activate, canary, automatic rollback, shadow
```

### What the real runs showed

The same staging procedure, 42 model calls, about 20,000 tokens and $0.011 at published prices per evaluation (priced from free-tier usage), about 160 seconds each.

| Release | Gate verdict | What the gate saw |
|---|---|---|
| `r1-baseline` | stored | 9/9 tickets handled as expected, 7/7 categories, 4/7 severities, 13/13 questions, 0 leaks |
| `r1-baseline` again | pass | every ticket decision identical; the share of required facts quoted in replies moved from 93 to 79 percent because two replies dropped a fact. That is the **noise floor** of this metric, so a one-case change is a warning and a fact-quoting change is never a failure on its own |
| `r2-shorter-reply` | pass | a benign prompt change: nothing moved except reply length and fact quoting (86 percent) |
| `r3-helpful-answerer` | **fail** | a well-meant prompt letting the assistant use general knowledge when the sources fall short: 13/13 became 7/13. Five of the six changed answers were stopped by the runtime citation check and came back as "withheld by policy checks"; one began by saying the sources had no information and then carried on. No restricted fact leaked (0), so a leak counter alone would have passed this release |
| `r4-renamed-categories` | **fail** | triage categories renamed in the prompt while the code still validates the old names: tickets handled as expected fell from 9/9 to 2/9, categories from 7/7 to 0/7, and 7 tickets were held |
| `r5-model-typo` | **stopped at stage 2** | `gemini-3.5-flash-lit`: the provider answered 404. No staging stack was started and no model was called |

**Live rollout** (four processes, `r1-baseline` active):

- The release admin API answered 401 without the key and with a wrong key. A release missing from the registry could not be loaded.
- `activate` took 0.23 s and `rollback` 0.20 s, with no restart; tickets before, after and after the rollback were served by `r1-baseline`, `r2-shorter-reply` and `r1-baseline`.
- **A healthy canary** (`r2-shorter-reply` at 30 percent): 4 of 12 tickets went to the canary, all released, the canary stayed on.
- **A bad canary** (`r4-renamed-categories` at 40 percent, deliberately not gated): 6 tickets were served by the bad release before the guard removed it, all of them held for review (the failure was safe, but six customers' tickets waited for a person); the 10 control tickets were all released. The recorded event: `canary bad-outcome rate 100% vs control 0% after 6 canary requests (limit +30%)`. Detection cost six tickets by design: the minimum sample is what stops one unlucky ticket from removing a good release.
- Latency per ticket varied between 6 and 44 seconds with the same release because of model latency; no release-versus-release latency claim is made.

**Shadow and the knowledge rollback, run live afterwards** (a second full run of `scripts/phase2_study.py`, log in `docs/results/phase2_rollout_study_real_rerun.log`): in shadow mode `r2-shorter-reply` received a copy of 6 active tickets and agreed with `r1-baseline` on action, category and severity in 6 of 6 with no errors, and the callers were served by `r1-baseline` only. On the knowledge service an unanswerable question was answered "I can't find that..." by `r1-baseline`, "withheld by policy checks" after `r3-helpful-answerer` was activated, and as before after one rollback call. The bad canary repeated (6 tickets held, removed by the guard) and the three tickets after the automatic rollback were all released by `r1-baseline`.

Limits: nine tickets and thirteen questions detect a broken release, not a slightly worse one; a model's run-to-run variation is of the same size as small prompt effects, which is why the rules above need a two-case drop. The gate's evaluation data is the system's own labelled set, so a prompt tuned until the gate passes can overfit it.

## Observability and SLOs

All open source: the OpenTelemetry Collector (contrib 0.162.0), Jaeger 1.76.0, Prometheus 3.15.0, Alertmanager 0.34.1 and Grafana 13.0.2, run with Podman (`observability/compose.yaml`). The services run on the host and send OTLP/HTTP to the collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; without it they behave exactly as before.

| Signal | How |
|---|---|
| **Traces** | every request is a server span that continues the caller's W3C `traceparent` (`deploylab/runtime.py`); outbound calls inject it (`deploylab/remote.py`; the A2A client already did). Spans from ADK, Requisite and the gateway's own `gateway.chat` join the same trace. Each span carries the correlation id and the serving release |
| **Metrics** | request counts and durations per service; ticket outcomes by action, release and role; hold reasons; gateway calls by caller, route, tier and outcome; tokens; cost; failed attempts; circuit-breaker state; serving release; probe results; drift verdict. Requisite's own `requisite.agent.*` metrics arrive without any code here |
| **Logs** | the JSON lines carry `trace_id` and `span_id`, so a log line leads to its trace and back. (A log store such as Loki is not part of this stack.) |

**SLOs are data** (`observability/slo.json`) and the Prometheus rules are generated from them (`scripts/make_slo_rules.py`), using the multi-window, multi-burn-rate method: an alert fires when the error budget is burning fast over a long window *and* a short one. Five SLOs: ticket availability, **ticket safe completion** (a held ticket counts as a failure), ticket latency (finished within 60 s), **answer quality** (synthetic probes), and no traffic on the degraded tier. Four more alerts watch for a restricted fact in a probe answer, a critical drift verdict, an open circuit breaker and degraded-tier serving. `slo.rules.yml` uses hour-scale windows; `slo.lab.rules.yml` has the same thresholds at minute scale so an alert can be seen to fire and clear inside a short run.

**The monitor** (`services/monitor_service.py`, `deploylab/monitor.py`) plays a user of the knowledge assistant on a schedule: six labelled probes in rotation (two answers, two restricted questions, one with no answer, one injection), judged by the same judge the release gate uses. It reduces the last results and the gateway's audit counters to the metrics of the system design lab's drift monitor and exports a verdict (0 stable, 1 warn, 2 critical). The baseline is saved once, by a person, with `scripts/save_baseline.py --yes`; the monitor never writes it.

```bash
venv/Scripts/python scripts/make_slo_rules.py && venv/Scripts/python scripts/make_dashboard.py
SLO_RULES=slo.lab.rules.yml podman-compose -f observability/compose.yaml up -d
venv/Scripts/python scripts/obs_smoke.py        # the stack alone, no model calls
venv/Scripts/python -u scripts/phase3_study.py  # traces, metrics, dashboard, two staged incidents (needs model quota)
```

### What has been verified

- **Offline (138 tests in the whole repo, 35 of them for this part):** a server span continues the caller's trace; metric labels stay bounded (twenty different ids become one `path` value); a log line inside a request carries the trace id; the remote clients forward the trace; the burn-rate arithmetic and windows; the probe judge and the drift verdicts for a leak, a quality drop, degraded-tier answers and a fallback rise; and a **contract test**: every metric any dashboard panel, SLO or rule mentions is one the code actually emits, converted to the name Prometheus will expose. A typo in a PromQL expression otherwise stays silent until the alert that should have fired does not.
- **The stack, live, without models** (`scripts/obs_smoke.py`, log in the blog folder): all six components up in 0.3 to 10.5 s; a counter, a histogram and a gauge sent over OTLP appear in Prometheus under the names the SLO rules use; the span appears in Jaeger; Prometheus loads the 34 generated rules with no errors (`promtool` also accepts both rule files); Grafana provisions the dashboard and both data sources.

### The staged incidents (real model)

A full run of `scripts/phase3_study.py` against the real Gemini API (log: `docs/results/phase3_study_real.log`). An earlier run of the same script against the provider simulator (`--sim`, described under Operations) is kept as `phase3_study_sim.log`; its incident timings were 144 s and 401 s for A and 127 s and 164 s for B.

- **Baseline and traffic.** The monitor's six probes all passed (zero leaks); eight tickets were released by `r1-baseline` in 6 to 34 s. Prometheus held 38 light-model calls, 7 cache hits, 17,144 input and 2,641 output tokens and $0.0117 of cost for the run.
- **One ticket is one trace across all four services.** The refund ticket produced a trace of 111 spans: 32 in the ticket service, 1 in the identity service, 11 in the gateway and 67 in the knowledge service. The trace id appears in log lines of all four.
- **The dashboard.** All 14 panels' queries ran through Grafana's own Prometheus data source. 12 returned data for every query, including the panel fed by Requisite's `requisite.agent.*` metrics; the burn-rate panel showed 4 of its 5 objectives (the latency ratio is under the traffic floor in a quiet run, by design) and the alerts table was empty because nothing was firing.
- **Incident A, quality:** a bad knowledge release (`r3-helpful-answerer`) activated without the gate. `DriftCritical` fired after 93 s (`correct_rate: critical`, correct ratio 0.727); the probes that failed were the restricted and no-answer questions (K02, K04, K12); no restricted fact leaked. After a rollback the alert cleared 420 s later (the 3-minute look-back plus the probe window). No probe-quality burn-rate alert fired: with 3 of 6 probes failing the burn is 8, below the 14.4 page threshold.
- **Incident B, dependency:** the gateway process killed. `SLOBurn_ticket_safe_completion_ticket` fired after 117 s and cleared 173 s after the restart; the two tickets sent meanwhile were held in about 2.7 s each. The **availability** error ratio was 0 (a held ticket is a `200`), the safe-completion ratio 1.0 and the probe error ratio 0.25. The ticket service's readiness still read ready (it answers from the last result). With the gateway down the probes also failed, so `DriftCritical` fired again about two minutes later and was still firing when the study stopped the services.
- **Alertmanager delivered** firing and resolved notifications for each alert to the webhook sink.

## Operations: capacity, guards, runbooks, drills

**Every result in this section came from a provider simulator, not the real API.** The free tier allows 500 requests a day per model, which one load sweep would spend in a minute, and a real provider cannot be made to fail
on demand. `sim/gemini_sim.py` answers in the Gemini wire format (the real Google SDK is pointed at it with `GOOGLE_GEMINI_BASE_URL`), enforces per-model minute and day quotas in the real error shape, has log-normal latency
and switchable faults (429 storm, 500s, slow, garbage, black hole). Its answers are scripted: triage by keywords, a tool-call flow for the resolver, source echoing for the knowledge assistant, and sensitivity to the two prompt
changes the drills use. Everything else is real: the four services and the monitor, the SDK, the gateway with its breakers and guards, and the observability stack with the generated SLO rules at minute-scale windows. These
numbers say how the system behaves against a rate-limited, failing provider. They say nothing about model quality.

| Piece | What it does |
|---|---|
| `deploylab/guards.py`, in the gateway service | **quota guard**: counts requests per model per provider day (kept across restarts) and refuses callers at per-caller thresholds, so low-priority work stops first and the last of the allowance goes to the traffic that matters; **spend guard**: a daily dollar cap with exempt callers |
| `POST /admin/freeze` on the ticket service | the kill switch: no new tickets, work in flight finishes, `Retry-After` on the refusal; also an alert, because a forgotten freeze is an outage |
| `docs/runbooks/` | one page per alert family: what it means, who is affected, the first five minutes (commands to paste), likely causes, when to escalate, afterwards. A test checks that every alert has a page, every endpoint a page calls exists in the services, and every metric a page mentions is emitted |
| `deploylab/capacity.py`, `docs/capacity.md` | the capacity arithmetic and scaling policy, tested against the measurements below |
| `docs/decisions.md` | seven operations decision records, each tied to a measurement |

```bash
venv/Scripts/python -u scripts/phase4_capacity.py      # four capacity experiments against the simulator
venv/Scripts/python -u scripts/phase4_incidents.py     # four incident drills, with the observability stack and background traffic
```

### Capacity (provider quota 60 requests a minute, about four calls per ticket, so a ceiling near 15 tickets a minute)

| Experiment | Result |
|---|---|
| One ticket service, 1 / 2 / 4 / 8 / 16 clients | 11.2 / 14.8 / 16.1 / 14.5 / 14.7 tickets a minute. **Throughput stops at 2 clients**; the median wait grows 5 / 7 / 14 / 28 / 52 s and the 95th percentile reaches 98 s. Little's law (16 clients at 14.7 a minute: 65 s average) matches |
| 1 / 2 / 3 ticket-service instances, 12 clients | 15.7 / 14.3 / 16.2 tickets a minute, medians 41 / 46 / 41 s. **More instances add nothing**: they share one quota |
| 1 versus 2 gateways (each with its own correct limiter) | 15.9 versus 12.9 tickets a minute, **17 of 36 held against 4**, 95th percentile 140 s against 60 s. **A second gateway makes it worse**: two limiters cannot see each other and together ask the provider for twice what it allows |
| A 140-request allowance runs out mid-run | no guard: 35 released, 13 held after 43 s on average, 5 provider refusals; guard at 85 percent: 29 released, 19 held after **3.8 s**, no refusals, 15 percent of the allowance kept for the knowledge assistant and the probes |

A first run of the gateway experiment showed two gateways as twice as fast (30 tickets a minute). The simulated heavy model had the light model's generous limits, so the extra throughput was calls spilling onto the expensive
tier and the local fallback (10 of 36 held). The real free tier gives the heavy model 20 requests a day; the final run uses that, and the first is kept as evidence of how easily scaling looks free.

### Incident drills (background traffic through every drill)

| Drill | What was injected | Alerts, seconds after injection | Customer impact | After the fix |
|---|---|---|---|---|
| D1 | the provider answers 429 to every model | `CircuitBreakerOpen` 81, `DegradedTierServing` 92, `DriftCritical` 92 | tickets served by the local fallback are held (11 of 12 in the first, longer run) | alerts clear after 76 s, 334 s, 360 s; 87 of 89 later tickets released |
| D2 | the light model takes 40 s longer per call | `CircuitBreakerOpen` 138 | 10 of 10 released, 95th percentile 74 s: failover hides a slow provider from customers | cleared after 148 s |
| D3 (first run, unaffected by the later fixes) | a gateway setting sends every request to the heavy tier (no release gate ran) | `CostPerTicketHigh` 298 | **none**: 54 of 54 released, 95th percentile 13 s | cleared after 154 s |
| D4 | a 300-request daily allowance, guard on | `QuotaGuardShedding` 225, `QuotaNearlyExhausted` 273 | 64 released, 71 held in under 7 s with a clear reason, no provider refusals | 92 of 92 released after the allowance resets |

What the drills taught, beyond the table:

- **A flapping signal defeats `for:`.** A circuit breaker re-closes to half-open every 30 s to test its route, so "open for a minute" never held: in the first run `CircuitBreakerOpen` did not fire in seven minutes of a 429 storm.
  And the drift verdict paged and resolved four times in one outage. Both alerts now look back (`max_over_time`, 2 and 3 minutes). The price is a later clear: `DriftCritical` cleared 334 s after the fix.
- **D3 is invisible to every quality and availability objective.** Nothing was slower, wrong or held; only the cost per ticket moved. The only thing that saw it was the cost alert, and a configuration change does not go through the release gate.
- **Alerts built on `rate(...[5m])` clear about five minutes after the fix**, however fast the fix was.
- **The safe-completion SLO never fired in the 429 storm.** Slow failures mean few tickets, and a long window dilutes them with the good minutes before. Burn-rate alerts depend on traffic volume.
- **Quota counts survive a restart**, as designed. A first run of D4 reset the counters before the old gateway had finished stopping; the old process wrote its high count back and the new one refused everything from the start.
  The harness now stops first and resets after.

Limits: a simulated provider; two background clients; one machine; thresholds (shed at 85 percent, the cost alert at three times $0.0011, burn-rate windows at minute scale) are calibrated for the lab, not for production traffic;
latencies come from the simulator's log-normal model.

## On Kubernetes

The same services run on a real Kubernetes cluster: **kind on Podman** (three nodes, Kubernetes 1.37, `kindnet` enforcing network policy), chosen because it is free, needs no account and no payment method, and the manifests apply unchanged to a
managed cluster. It is not a hosted cloud, and the cluster holds no model API key: the model API is the provider simulator running as a pod (Operations section), so what these runs measure is how the platform treats the system.

```bash
winget install Kubernetes.kind Kubernetes.kubectl                      # both Apache-2.0; podman machine set --rootful once
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --config deploy/kind-cluster.yaml
python scripts/make_k8s.py && python scripts/make_k8s_secrets.py        # manifests (generated) and a Secret (never committed)
kubectl apply -f deploy/k8s/
python -u scripts/phase5_cluster.py                                     # posture, network policy
python -u scripts/phase5_events.py                                      # rolling updates, crashes, a node drain, under load
```

`deploy/k8s/` is generated by `scripts/make_k8s.py` so the things that must agree do, and 23 tests check the result: every container non-root with a read-only filesystem, all capabilities dropped and a seccomp profile; startup, liveness
and readiness probes (liveness never looks at dependencies); each pod mounts only the secret files it needs, so **the model key is in exactly one pod**; default-deny network policy with each service admitting only its callers; the gateway
as **one replica with a Recreate strategy** (its limiter, breakers and counters are per process); the ticket service as two replicas with `maxUnavailable: 0`, a pod disruption budget and a spread across nodes; the namespace enforcing the
`restricted` Pod Security standard.

### What the runs showed (simulated provider)

- **A ticket from outside the cluster**, over TLS through a NodePort, was released with its policy lookup crossing the ticket, identity, knowledge and gateway pods; the correlation id appears in the logs of all four.
- **The platform refuses what the manifests forbid.** All four app pods run as uid 10001 with writes to the image refused; each sees only its own secret files (only the gateway has the model key). Applying a privileged pod was rejected by the API
  server itself (`violates PodSecurity "restricted:latest"`).
- **Network policy.** From an unlabelled pod: the ticket API connects (it is a front door), the gateway and the knowledge service are blocked. A pod that merely *labels itself* as the ticket service connects to the gateway: labels are
  not identity, and keeping anyone from creating pods with those labels is RBAC's job.
- **Rolling updates under load** (four closed-loop clients, five restarts of the ticket deployment each, tickets that fail counted):

  | Settings | Tickets | Failed | How |
  |---|---|---|---|
  | no readiness gate, no drain, 1 s grace | 95 | **7** | connections dropped mid-request |
  | readiness gate, drain, 75 s grace, but new tickets refused while draining | 97 and 123 | 2 and 3 | clean `503 draining` with `Retry-After` |
  | the same, and tickets still served while draining | 121 | **0** | |

  The middle row is the lesson. The refusal I traced arrived 0.5 s after its pod's stop signal and was answered in under a millisecond: Kubernetes needs a moment to stop sending traffic to a terminating pod, and a handler that refuses as soon as it
  starts draining turns that moment into errors. Phase 1 had the service refuse new tickets while draining; that was right for one process behind nothing and wrong behind a Service. Now `/readyz` reports `draining` (the cue to stop sending)
  and the service goes on serving until the listener closes.
- **Crashes** (a pod killed with no grace, three times each): the ticket service lost nothing (37 of 37 released, never unready, 4.1 s to be whole again). Killing the only gateway pod cost two held tickets in 36, with the ticket service unready
  for 5.1, 2.5 and 5.1 s, and the gateway replaced in 4.1 s on average.
- **Draining the node that hosts the gateway:** 16 of 16 released, the ticket service unready for 2.5 s, everything resettled in about 13 s; a pod disruption budget (at least one ticket pod) was in place.

Recovery times are for a local cluster with the image already on every node; a managed cluster adds image pulls and node start-up. The first cluster run took "rolled out" at face value and reported a 4 s recovery that was really the controller not
having noticed the crash yet; the second run decides recovery by waiting for a new Ready pod and probes `/readyz` every half second. Both logs are kept in the blog folder.

Not on the cluster: the observability stack and the monitor (they need the baseline and the collector wired in), the local fallback model (its address is the host's `localhost`), and horizontal autoscaling (Operations: scaling the ticket service does not add throughput).

## Run it

```bash
python -m venv venv && venv/Scripts/pip install -r requirements.txt
cp .env.example .env                       # add GEMINI_API_KEY
venv/Scripts/python -m pytest tests -q     # 196 offline tests, no network
venv/Scripts/python -u scripts/phase1_study.py   # real run: four OS processes, real models

# the same system as containers (Podman and podman-compose, both open source)
venv/Scripts/python scripts/vendor_system.py            # export the system under deployment into the build context
venv/Scripts/python scripts/make_secrets.py             # secret files, private CA, certificate
podman build --format docker -f deploy/Dockerfile -t production-deployment-lab:phase1 --build-arg APP_VERSION=phase1 .
APP_VERSION=phase1 podman-compose -f deploy/compose.yaml up -d
venv/Scripts/python -u scripts/container_study.py       # real run against the containers
```

## What the real run showed

Gemini free tier, `gemini-3.5-flash-lite` pinned, local `llama3.2:1b` as the degraded tier.

- **No start order needed.** All four started at once and were ready in 0.8 s (gateway), 1.1 s (identity), 3.2 s (tickets) and 11.4 s (knowledge, which waits for the gateway before it can embed its documents).
- **Least privilege.** Only the gateway process was given the model key; the others received only their own secret files.
- **One ticket, four services, one correlation id.** A refund ticket from the support operator ran triage, an MCP tool loop and a policy lookup over A2A with an exchanged token; the same correlation id appears in the logs of the ticket, gateway, identity and knowledge services. The knowledge audit shows who the policy was answered for: the support operator received it (`SUP-001`), the engineer received "not found". Reply wording varies, because the model summarises; the audit is the evidence.
- **The front door refuses.** No token, a forged token and a token for another audience all got `401` with a distinct reason.
- **Bad configuration refuses to start.** A gateway with no configuration exited with code 2 and a message listing both missing settings.
- **Soft versus hard dependency.** With the knowledge service stopped, the ticket service noticed within 2.9 s, stayed ready (200), and held a ticket that needed a policy lookup (`capability_unavailable: ask_policy`). With the gateway stopped, the knowledge and ticket services reported unready within about 2.4 s and 2.3 s, `/healthz` stayed 200 everywhere, `/readyz` answered in 220 to 640 ms, and both services were ready again 1.9 s and 1.3 s after the gateway returned, without being restarted.
- **A stop signal in the middle of a ticket.** The ticket in flight finished with HTTP 200 and the process exited 0 after 4.9 s; during the drain `/readyz` reported `draining` (in this first version a new ticket also got `503`; Phase 5 found that wrong behind a load balancer and the service now keeps serving until the listener closes). A hard kill at the same moment lost the in-flight ticket (`connection lost`).
- **A design hazard that only appears when the gateway is shared.** In the first run the shared gateway answered the engineer's final reply from a semantic-cache hit on the support operator's earlier request (similarity 0.978), because both tickets belong to the same tenant. In the system design lab the ticket path ran without a cache, so the hazard never showed. The gateway now decides per caller who may use the cache (see above); the next run showed zero cache hits for the ticket service.

### The same checks against the containers

Podman 5.8.3 on Windows (WSL2 machine), rootless. Same models, same tickets.

- **Runtime view.** All four containers `healthy` by their own health checks, uid 10001, read-only root filesystem (`touch /app/x` fails), only the gateway container has the model key and only the gateway has the public-roots bundle.
- **Tickets.** Both operators' refund tickets were `released` (4 model calls each); the knowledge container's logs carry both correlation ids, so the call crossed TLS and A2A inside the container network.
- **A dependency container stops and returns.** With the gateway container stopped, the ticket service reported unready after 2.1 s and was ready again 2.1 s after the gateway came back, with nothing else restarted.
- **The platform's stop signal.** `podman stop` during a ticket: readiness went `ready` then `draining`, the in-flight ticket finished `released`, the container exited 0 after 6.2 s. `podman kill` at the same moment: exit code 137, in-flight ticket lost.
- **Observed, not explained.** Stopping the gateway container took 5.7 s, 6.0 s and 32.2 s in three runs. In the slow one the log shows `inflight: 0` and then 26 s of "waiting for connections to close", so the wait is on idle keep-alive connections held by other services, not on work. It stays inside the grace period but is worth chasing before relying on short stop timeouts.
- **Two things only the container run found.** The plain-http agent card rejection above, and the gateway's trust bundle.

Limits: one machine, free-tier quota, synthetic data, a small number of tickets. Latencies are dominated by the shared 15-requests-per-minute limit and model calls, not by the services.
