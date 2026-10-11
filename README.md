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

**Not yet run live** (implemented and covered by offline tests): shadow mode and the knowledge service's activate-and-rollback. Their first live run used the day's free-tier quota (`429 RESOURCE_EXHAUSTED`), which invalidated their numbers, so none are reported. `python -u scripts/phase2_study.py --sections 5,6` repeats them.

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

### Not yet run live

The staged incidents: a trace across all four services for a real ticket, the dashboard's panels with real data, a bad knowledge release (does the drift monitor read critical and does the quality alert fire?) and a dead gateway (which alerts fire, how long after, do they clear after the fix, does Alertmanager deliver both). They need healthy model answers, and the free tier's 500 requests per day per model (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`) were used up by the earlier phases' runs. `scripts/phase3_study.py` is written and waits for the quota to reset.

Two properties of the method are visible without any run. A 90 percent objective has an error budget of 10 percent, so its largest possible burn rate is 10, below the 14.4 page threshold: the safe-completion SLO can only ever raise the ticket-level alert, never a page, while the 95 percent probe SLO (largest burn 20) can page. And a gateway that is down stops emitting its own metrics, so its failure shows up in its callers' errors and in readiness, not in its own breaker gauge.

## Run it

```bash
python -m venv venv && venv/Scripts/pip install -r requirements.txt
cp .env.example .env                       # add GEMINI_API_KEY
venv/Scripts/python -m pytest tests -q     # 138 offline tests, no network
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
- **A stop signal in the middle of a ticket.** The ticket in flight finished with HTTP 200 and the process exited 0 after 4.9 s; during the drain `/readyz` reported `draining` and a new ticket got `503`. A hard kill at the same moment lost the in-flight ticket (`connection lost`).
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
