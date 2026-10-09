"""Ticket service: the support-ticket platform behind an HTTP API.

    POST /tickets   Authorization: Bearer <operator token for audience "ticket-agent">
                    {"id": "...", "text": "..."}  ->  the platform's outcome (released | held | escalated)

The operator's identity comes from the verified token, never from the request body. The ticket platform is the
#17 `TicketPlatform` unchanged: its model calls go to the gateway service, its policy lookups go to the
knowledge service over A2A with an exchanged token from the identity service, and its tools run over MCP in a
child process this service owns.

Readiness: the gateway and the identity service are HARD dependencies (without them nothing can be processed).
The knowledge service is SOFT: if it is down the platform still triages and replies, and holds tickets that need
a policy lookup, so it is reported but does not take this service out of rotation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_layer.client import KnowledgeClient
from deploylab import logs
from deploylab.config import Common, Env
from deploylab.remote import RemoteGatewayProvider, RemoteIdentity
from deploylab.runtime import Lifecycle, make_app, serve
from identity.tokens import AuthError, verify
from knowledge.corpus import USERS
from tickets.data import Ticket
from tickets.platform import TicketPlatform
from tickets.policy_tool import PolicyTool

log = logging.getLogger("tickets")
SERVICE = "ticket-agent"
MAX_TEXT = 4000


class Settings:
    def __init__(self, env: Env) -> None:
        self.common = Common(env, "tickets", 8104)
        self.token_secret = env.secret("TOKEN_SECRET", min_length=32)
        self.gateway_url = env.str("GATEWAY_URL", required=True)
        self.gateway_key = env.secret("GATEWAY_SERVICE_KEY", min_length=16)
        self.knowledge_url = env.str("KNOWLEDGE_URL", required=True)
        self.identity_url = env.str("IDENTITY_URL", required=True)
        self.backend = env.str("ORCHESTRATOR", "adk")
        self.max_concurrent = env.int("MAX_CONCURRENT_TICKETS", 4, minimum=1)
        self.max_waiting = env.int("MAX_WAITING_TICKETS", 8, minimum=0)
        env.done()


async def _ready(url: str) -> object:
    async with httpx.AsyncClient(timeout=2.5) as client:
        r = await client.get(f"{url.rstrip('/')}/readyz")
    return True if r.status_code == 200 else f"not ready ({r.status_code})"


def create_app(settings: Settings, platform: TicketPlatform = None, remote: RemoteGatewayProvider = None):
    common = settings.common
    lc = Lifecycle(common.service, common.version)
    secret = settings.token_secret.reveal().encode()
    if platform is None:
        remote = RemoteGatewayProvider(settings.gateway_url, SERVICE, settings.gateway_key)
        ptool = PolicyTool(KnowledgeClient(settings.knowledge_url), RemoteIdentity(settings.identity_url))
        platform = TicketPlatform(remote, backend=settings.backend, policy_tool=ptool)
    slots = asyncio.Semaphore(settings.max_concurrent)
    state = {"waiting": 0}

    async def submit(request: Request) -> JSONResponse:
        header = request.headers.get("authorization", "")
        try:
            if not header.lower().startswith("bearer "):
                raise AuthError("missing_token")
            claims = verify(secret, header[7:].strip(), SERVICE)
        except AuthError as exc:
            logs.event(log, "ticket refused", level=logging.WARNING, reason=exc.reason)
            return JSONResponse({"error": "unauthorized", "reason": exc.reason}, status_code=401)
        try:
            data = await request.json()
            text = str(data["text"]).strip()
            ticket_id = str(data.get("id") or "").strip() or logs.correlation_id.get()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad_request", "detail": "send JSON with a 'text' field"}, status_code=400)
        if not text or len(text) > MAX_TEXT:
            return JSONResponse({"error": "bad_request", "detail": f"text must be 1..{MAX_TEXT} characters"}, status_code=400)
        if lc.draining:
            return JSONResponse({"error": "draining"}, status_code=503, headers={"Retry-After": "5"})
        if state["waiting"] >= settings.max_waiting:       # backpressure: refuse instead of queueing without bound
            return JSONResponse({"error": "overloaded"}, status_code=429, headers={"Retry-After": "5"})
        user = USERS.get(claims.sub)
        ticket = Ticket(ticket_id, user.org if user else "unknown", text, "", "")
        corr = logs.correlation_id.get()
        state["waiting"] += 1
        try:
            await slots.acquire()
        finally:
            state["waiting"] -= 1                 # also runs if the caller disconnects while queued
        try:
            outcome = await asyncio.to_thread(platform.handle, ticket, claims.sub, header[7:].strip(), corr)
        finally:
            slots.release()
        body = asdict(outcome)
        body["triage"] = outcome.triage.model_dump() if outcome.triage is not None else None    # a pydantic model inside a dataclass
        logs.event(log, "ticket handled", ticket=ticket.id, operator=claims.sub, action=outcome.action,
                   model_calls=outcome.model_calls, latency_s=round(outcome.latency_s, 2))
        return JSONResponse(body)

    lc.checks["gateway"] = lambda: _ready(settings.gateway_url)
    lc.checks["identity"] = lambda: _ready(settings.identity_url)
    lc.soft_checks["knowledge"] = lambda: _ready(settings.knowledge_url)
    app = make_app(lc, [Route("/tickets", submit, methods=["POST"])])
    return app, lc, platform


def main() -> None:
    try:
        settings = Settings(Env())
    except Exception as exc:  # ConfigError
        logs.setup("tickets")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(settings.common.service, settings.common.log_level)
    app, lc, platform = create_app(settings)
    try:
        serve(app, lc, settings.common)
    finally:
        platform.close()


if __name__ == "__main__":
    main()
