"""Ticket service: the support-ticket platform behind an HTTP API, with releases.

    POST /tickets   Authorization: Bearer <operator token for audience "ticket-agent">
                    {"id": "...", "text": "..."}  ->  the platform's outcome (released | held | escalated)
    /admin/releases/...   load, activate, canary, shadow and roll back releases (X-Admin-Key)

The operator's identity comes from the verified token, never from the request body. The ticket platform is the
#17 `TicketPlatform` unchanged: its model calls go to the gateway service, its policy lookups go to the
knowledge service over A2A with an exchanged token from the identity service, and its tools run over MCP in a
child process this service owns.

Releases: every loaded release is its own built platform (its own prompts, its own tool process). Requests go to
the active release, or, during a canary, a stable share goes to the canary release. A shadow release gets a copy
of active traffic after the real answer has been sent and is only compared. Every response says which release
served it.

Readiness: the gateway and the identity service are HARD dependencies (without them nothing can be processed).
The knowledge service is SOFT: if it is down the platform still triages and replies, and holds tickets that need
a policy lookup, so it is reported but does not take this service out of rotation.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_layer.client import KnowledgeClient
from deploylab import logs
from deploylab.config import Common, Env
from deploylab.releases import Registry, ReleaseSet, admin_routes, patched_prompts
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
DEFAULT_RELEASES = Path(__file__).resolve().parent.parent / "releases"


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
        self.releases_dir = Path(env.str("RELEASES_DIR", str(DEFAULT_RELEASES)))
        self.active_release = env.str("ACTIVE_RELEASE", "r1-baseline")
        self.admin_key = env.secret("ADMIN_KEY", required=False, min_length=16)      # without it the admin API is not mounted
        env.done()


async def _ready(url: str) -> object:
    async with httpx.AsyncClient(timeout=2.5) as client:
        r = await client.get(f"{url.rstrip('/')}/readyz")
    return True if r.status_code == 200 else f"not ready ({r.status_code})"


def create_app(settings: Settings, platform: TicketPlatform = None, remote: RemoteGatewayProvider = None):
    """Returns (app, lifecycle, release_set). Passing `platform` runs a single unnamed release (used by tests)."""
    common = settings.common
    lc = Lifecycle(common.service, common.version)
    secret = settings.token_secret.reveal().encode()
    registry = Registry(settings.releases_dir)
    rs = ReleaseSet(common.service)
    background: set = set()

    if platform is None:
        remote = RemoteGatewayProvider(settings.gateway_url, SERVICE, settings.gateway_key)
        ptool = PolicyTool(KnowledgeClient(settings.knowledge_url), RemoteIdentity(settings.identity_url))

        def build(release):
            with patched_prompts(release.prompts):               # the platform captures these prompts when it is built
                built = TicketPlatform(remote, backend=settings.backend, policy_tool=ptool)
                built._build()
            return built

        initial = registry.get(settings.active_release)
        rs.add(initial, build(initial), make_active=True)
    else:
        from deploylab.releases import Release
        build = None
        rs.add(Release(id="test", prompts={}, models={}), platform, make_active=True)

    slots = asyncio.Semaphore(settings.max_concurrent)
    state = {"waiting": 0}

    async def shadow_run(ticket, operator, token, corr, primary, release_id) -> None:
        """Run the shadow release on a copy of the request. Never changes what the caller received."""
        try:
            sh = await asyncio.to_thread(rs.component(release_id).handle, ticket, operator, token, f"shadow-{corr}")
            rs.record_shadow(sh.action == primary.action,
                             bool(sh.triage and primary.triage and sh.triage.category == primary.triage.category),
                             bool(sh.triage and primary.triage and sh.triage.severity == primary.triage.severity), False)
        except Exception:  # noqa: BLE001 - a broken candidate must show up as a count, not as a failed request
            rs.record_shadow(False, False, False, True)
        finally:
            slots.release()

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
        release_id, role = rs.choose(ticket_id)            # the ticket id decides the side, so a retry lands on the same release
        state["waiting"] += 1
        try:
            await slots.acquire()
        finally:
            state["waiting"] -= 1                 # also runs if the caller disconnects while queued
        token = header[7:].strip()
        try:
            outcome = await asyncio.to_thread(rs.component(release_id).handle, ticket, claims.sub, token, corr)
        except Exception:
            rs.record(release_id, role, "error", 0.0)
            raise
        finally:
            slots.release()
        event = rs.record(release_id, role, outcome.action, outcome.latency_s)
        if event:
            logs.event(log, "canary rolled back automatically", level=logging.WARNING, **{k: v for k, v in event.items() if k != "type"})
        # Shadow: only on active traffic, only if a slot is free (shadow work must never starve real work).
        if rs.shadow and role in ("active", "control") and rs.shadow != release_id and not slots.locked():
            await slots.acquire()
            task = asyncio.get_running_loop().create_task(shadow_run(ticket, claims.sub, token, corr, outcome, rs.shadow))
            background.add(task)
            task.add_done_callback(background.discard)
        body = asdict(outcome)
        body["triage"] = outcome.triage.model_dump() if outcome.triage is not None else None    # a pydantic model inside a dataclass
        body["release"] = {"id": release_id, "digest": rs.meta[release_id]["digest"], "role": role}
        logs.event(log, "ticket handled", ticket=ticket.id, operator=claims.sub, action=outcome.action,
                   model_calls=outcome.model_calls, latency_s=round(outcome.latency_s, 2), release=release_id, role=role)
        return JSONResponse(body)

    lc.checks["gateway"] = lambda: _ready(settings.gateway_url)
    lc.checks["identity"] = lambda: _ready(settings.identity_url)
    lc.soft_checks["knowledge"] = lambda: _ready(settings.knowledge_url)
    routes = [Route("/tickets", submit, methods=["POST"])]
    if settings.admin_key.reveal() and build is not None:
        routes += admin_routes(rs, registry, build, settings.admin_key.reveal())
    app = make_app(lc, routes)
    return app, lc, rs


def main() -> None:
    try:
        settings = Settings(Env())
    except Exception as exc:  # ConfigError
        logs.setup("tickets")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(settings.common.service, settings.common.log_level)
    app, lc, rs = create_app(settings)
    try:
        serve(app, lc, settings.common)
    finally:
        for component in list(rs.components.values()):
            component.close()


if __name__ == "__main__":
    main()
