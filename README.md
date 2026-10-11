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

## Run it

```bash
python -m venv venv && venv/Scripts/pip install -r requirements.txt
cp .env.example .env                       # add GEMINI_API_KEY
venv/Scripts/python -m pytest tests -q     # 52 offline tests, no network
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
