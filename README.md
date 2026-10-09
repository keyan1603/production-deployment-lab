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

`deploy/Dockerfile` and `deploy/compose.yaml` describe the same four services as unprivileged, read-only containers with file-mounted secrets. **They have not been built or run yet** (no container runtime was available when they were written); `tests/test_deploy_files.py` checks that they are consistent with the code.

## Run it

```bash
python -m venv venv && venv/Scripts/pip install -r requirements.txt
cp .env.example .env                       # add GEMINI_API_KEY
venv/Scripts/python -m pytest tests -q     # 48 offline tests, no network
venv/Scripts/python -u scripts/phase1_study.py   # real run: four processes, real models
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

Limits: one machine, free-tier quota, synthetic data, a small number of tickets. Latencies are dominated by the shared 15-requests-per-minute limit and model calls, not by the services.
