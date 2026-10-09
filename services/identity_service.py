"""Identity service: login and token exchange, as its own process.

Lab-grade, exactly as in the AI System Design lab (shared-secret HMAC tokens, no passwords). What changes
here is the deployment shape: the signing secret lives in this service and the services that verify tokens,
and nowhere else; it is read from the environment or a mounted file, never from code.
A production system replaces this service with an OIDC / OAuth 2.1 provider and asymmetric keys.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from deploylab import logs
from deploylab.config import Common, Env
from deploylab.runtime import Lifecycle, make_app, serve
from identity.tokens import AuthError, IdentityProvider
from knowledge.corpus import USERS

log = logging.getLogger("identity")

# Per-service ceilings: the most access a service may ever lend when it acts for a user.
DEFAULT_CEILINGS = {"ticket-agent": {"employee", "support"}}


def build(env: Env) -> tuple[Common, IdentityProvider]:
    common = Common(env, "identity", 8101)
    secret = env.secret("TOKEN_SECRET", min_length=32)
    ttl = env.float("TOKEN_TTL_S", 300.0, minimum=1.0)
    env.done()
    return common, IdentityProvider(secret.reveal().encode(), USERS, DEFAULT_CEILINGS, ttl_s=ttl)


def create_app(common: Common, idp: IdentityProvider):
    lc = Lifecycle(common.service, common.version)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def login(request: Request) -> JSONResponse:
        data = await body(request)
        username, audience = data.get("username", ""), data.get("audience", "")
        if username not in USERS or not audience:
            logs.event(log, "login refused", reason="unknown_user_or_audience")
            return JSONResponse({"error": "unauthorized", "reason": "unknown_user"}, status_code=401)
        logs.event(log, "login", subject=username, audience=audience)
        return JSONResponse({"token": idp.login(username, audience)})

    async def exchange(request: Request) -> JSONResponse:
        data = await body(request)
        try:
            token = idp.exchange(data.get("token", ""), data.get("calling_service", ""), data.get("target_audience", ""))
        except AuthError as exc:
            logs.event(log, "exchange refused", reason=exc.reason, calling_service=data.get("calling_service", ""))
            return JSONResponse({"error": "unauthorized", "reason": exc.reason}, status_code=401)
        logs.event(log, "exchange", calling_service=data.get("calling_service", ""), target=data.get("target_audience", ""))
        return JSONResponse({"token": token})

    app = make_app(lc, [Route("/login", login, methods=["POST"]), Route("/exchange", exchange, methods=["POST"])])
    return app, lc


def main() -> None:
    try:
        common, idp = build(Env())
    except Exception as exc:  # ConfigError: report every problem and exit non-zero
        logs.setup("identity")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(common.service, common.log_level)
    app, lc = create_app(common, idp)
    serve(app, lc, common)


if __name__ == "__main__":
    main()
