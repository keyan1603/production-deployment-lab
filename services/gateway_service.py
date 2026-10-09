"""Gateway service: the AI gateway from the AI System Design lab, as a network service.

Every other service reaches a model through here, and ONLY here holds the model provider's API key. That
gives one place for the rate limiter that guards the provider quota, the circuit breakers, the cache, the
per-tenant budgets and the audit trail, instead of one copy per service.

    POST /v1/chat    messages (+ tool schemas) in, the model's response out; tools run in the caller
    POST /v1/embed   texts in, vectors out
    GET  /v1/status  route and circuit-breaker state, for operators

Callers authenticate as services (a per-service key). The tenant and operator they send are trusted because
the caller is trusted; the end user's own token is verified by the services that act on it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from typing import Optional

from requisite.core.context import RequestContext, request_context
from requisite.core.exceptions import CostLimitException, ProviderException
from requisite.rag.embeddings.gemini import GeminiEmbeddingProvider
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from deploylab import logs
from deploylab.config import Common, Env
from deploylab.runtime import Lifecycle, make_app, serve
from deploylab.serde import decode_messages, decode_tools, encode_response
from gateway.factory import build_gateway
from gateway.provider import GatewayExhausted
from gateway.routing import LIGHT

log = logging.getLogger("gateway")
MAX_AUDIT_RECORDS = 10_000


class Settings:
    def __init__(self, env: Env) -> None:
        self.common = Common(env, "gateway", 8102)
        self.gemini_key = env.secret("GEMINI_API_KEY")
        raw = env.secret("GATEWAY_SERVICE_KEYS")
        self.keys: dict[str, str] = {}
        for part in raw.reveal().split(","):
            if part.strip():
                name, _, key = part.partition("=")
                if not name.strip() or len(key.strip()) < 16:
                    env.problems.append("GATEWAY_SERVICE_KEYS entries look like name=key with keys of 16+ characters")
                    break
                self.keys[name.strip()] = key.strip()
        self.requests_per_minute = env.int("REQUESTS_PER_MINUTE", 15, minimum=1)
        self.tenant_budget_usd = env.float("TENANT_BUDGET_USD", 0.0)
        self.cache = env.str("CACHE_ENABLED", "true").lower() == "true"
        self.audit_path = env.str("AUDIT_PATH", "")
        # Which callers may be answered from the semantic cache. A cached answer is only safe to replay for a caller
        # whose answers depend on nothing but the prompt and a cache scope the caller sets (the knowledge service
        # puts the user's access fingerprint in the scope). The ticket service's answers depend on tool results that
        # differ by operator, so by default it is never served from, or stored in, the cache.
        self.cache_services = {s.strip() for s in env.str("CACHE_SERVICES", "knowledge-agent").split(",") if s.strip()}
        self.pin_light = env.str("PIN_LIGHT", "false").lower() == "true"
        env.done()


def create_app(settings: Settings, gw=None, embedder=None):
    common = settings.common
    lc = Lifecycle(common.service, common.version)
    if gw is None:
        os.environ["GEMINI_API_KEY"] = settings.gemini_key.reveal()       # read by the provider SDKs inside this process only
        os.environ["REQUESTS_PER_MINUTE"] = str(settings.requests_per_minute)
        gw, _ = build_gateway(use_cache=settings.cache, tenant_budget_usd=settings.tenant_budget_usd or None,
                              audit_path=settings.audit_path or None,
                              classifier=(lambda m, t, s: LIGHT) if settings.pin_light else None)
    if embedder is None:
        embedder = GeminiEmbeddingProvider(api_key=settings.gemini_key.reveal(), model=os.environ.get("EMBEDDING_MODEL", "gemini-embedding-2"))

    def caller(request: Request) -> Optional[str]:
        """The authenticated service name, or None."""
        name, key = request.headers.get("x-service", ""), request.headers.get("x-service-key", "")
        expected = settings.keys.get(name)
        if expected and key and hmac.compare_digest(expected, key):
            return name
        return None

    def tier_of(route_name: Optional[str]) -> str:
        return next((r.tier for r in gw.routes if r.name == route_name), "")

    def audit_facts(corr: str) -> dict:
        for rec in reversed(gw.audit.records[-300:]):
            if rec.correlation_id == corr:
                return {"correlation_id": corr, "route": rec.route, "tier": tier_of(rec.route), "outcome": rec.outcome,
                        "cost_usd": rec.cost_usd, "input_tokens": rec.input_tokens, "output_tokens": rec.output_tokens,
                        "latency_ms": rec.latency_ms}
        return {"correlation_id": corr}

    def trim_audit() -> None:
        with gw.audit._lock:
            if len(gw.audit.records) > MAX_AUDIT_RECORDS:
                del gw.audit.records[: MAX_AUDIT_RECORDS // 2]

    async def chat(request: Request) -> JSONResponse:
        service = caller(request)
        if service is None:
            logs.event(log, "caller rejected", level=logging.WARNING)
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            data = await request.json()
            messages, tools = decode_messages(data["messages"]), decode_tools(data.get("tools", []))
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad_request", "detail": "malformed chat request"}, status_code=400)
        corr = request.headers.get("x-correlation-id", "")
        ctx = RequestContext(user=request.headers.get("x-operator") or None, tenant=request.headers.get("x-tenant") or None,
                             correlation_id=corr or None, attributes={"service": service})
        kwargs = dict(data.get("options", {}))
        if data.get("temperature") is not None:
            kwargs["temperature"] = data["temperature"]
        if data.get("model"):
            kwargs["model"] = data["model"]
        if service not in settings.cache_services:
            kwargs["no_cache"] = True
        try:
            with request_context(ctx):
                resp = await gw.achat(messages, tools=tools or None, **kwargs)
        except CostLimitException as exc:
            return JSONResponse({"error": "budget", "detail": str(exc)[:200]}, status_code=402)
        except GatewayExhausted as exc:
            return JSONResponse({"error": "exhausted", "detail": str(exc)[:200]}, status_code=503)
        except ProviderException as exc:
            return JSONResponse({"error": "provider", "detail": str(exc)[:200]}, status_code=502)
        finally:
            trim_audit()
        return JSONResponse({"response": encode_response(resp), "audit": audit_facts(corr)})

    async def embed(request: Request) -> JSONResponse:
        if caller(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            texts = (await request.json())["texts"]
            vectors = await asyncio.to_thread(embedder.embed, texts)
        except KeyError:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        except Exception as exc:  # noqa: BLE001 - the caller fails closed on any embedding failure
            logs.event(log, "embedding failed", level=logging.ERROR, error=type(exc).__name__)
            return JSONResponse({"error": "provider", "detail": type(exc).__name__}, status_code=502)
        return JSONResponse({"vectors": vectors})

    async def status(request: Request) -> JSONResponse:
        if caller(request) is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"routes": [{"name": r.name, "tier": r.tier, "breaker": r.breaker.state} for r in gw.routes],
                             "audit": gw.audit.summary()})

    def routes_available() -> object:
        open_all = all(r.breaker.state == "open" for r in gw.routes)
        return "every route's circuit breaker is open" if open_all else True

    lc.checks["routes"] = routes_available
    app = make_app(lc, [Route("/v1/chat", chat, methods=["POST"]), Route("/v1/embed", embed, methods=["POST"]),
                        Route("/v1/status", status)])
    return app, lc, gw


def main() -> None:
    try:
        settings = Settings(Env())
    except Exception as exc:  # ConfigError
        logs.setup("gateway")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(settings.common.service, settings.common.log_level)
    app, lc, _ = create_app(settings)
    serve(app, lc, settings.common)


if __name__ == "__main__":
    main()
