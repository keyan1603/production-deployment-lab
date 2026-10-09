"""Offline service tests over REAL HTTP on localhost, with scripted models (no API keys, no network)."""

import time

import httpx
import pytest
from requisite.core.cost_limiter import CostLimiter
from requisite.core.exceptions import CostLimitException, ProviderException
from requisite.core.interfaces import ChatResponse, Message, ToolCall, Usage
from requisite.providers.base import BaseProvider
from requisite.rag.base import BaseEmbeddingProvider
from requisite.core.context import RequestContext, request_context
from requisite.tools.base import Tool

from a2a_layer.server import ServerThread, free_port
from deploylab.config import Env, Secret
from deploylab.remote import RemoteEmbeddingProvider, RemoteGatewayProvider, RemoteIdentity
from gateway.audit import AuditLog
from gateway.provider import GatewayExhausted, GatewayProvider
from gateway.resilience import CircuitBreaker
from gateway.routing import LIGHT, Route
from identity.tokens import AuthError, issue
from services import gateway_service, identity_service, knowledge_service, ticket_service
from guardrails.checks import TriageResult
from tickets.platform import Outcome

SECRET = "s" * 40
KEY = "k" * 20


class Scripted(BaseProvider):
    """A model that answers from a script; records what the gateway really sent."""

    def __init__(self, fail=False):
        super().__init__(api_key="x", model="scripted")
        self.fail, self.seen = fail, []

    @property
    def name(self):
        return "scripted"

    def _answer(self, messages, tools):
        if self.fail:
            raise ProviderException("upstream down", provider="scripted")
        self.seen.append({"n": len(messages), "tools": [t.name for t in (tools or [])]})
        if tools:
            return ChatResponse(content="", model="scripted", provider="scripted", usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                                tool_calls=[ToolCall(id="c1", name=tools[0].name, arguments={"x": "1"}, provider_data=b"sig")])
        return ChatResponse(content="hello", model="scripted", provider="scripted", usage=Usage(prompt_tokens=5, completion_tokens=5, total_tokens=10))

    def chat(self, messages, *, model=None, temperature=None, tools=None, response_model=None, **kw):
        return self._answer(messages, tools)

    async def achat(self, messages, *, model=None, temperature=None, tools=None, response_model=None, **kw):
        return self._answer(messages, tools)

    def stream(self, *a, **k):
        raise NotImplementedError

    def astream(self, *a, **k):
        raise NotImplementedError


class FakeEmbedder(BaseEmbeddingProvider):
    name = "fake"

    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


def _cost(usage, model):
    return 1.0


def make_gateway(fail=False, tenant_budget=None):
    prov = Scripted(fail)
    route = Route("scripted-light", prov, LIGHT, _cost, CircuitBreaker(failure_threshold=1, reset_timeout_s=60), timeout_s=5)
    gw = GatewayProvider([route], audit=AuditLog(),
                         tenant_budgets=(lambda t: CostLimiter(budget_usd=tenant_budget, cost_fn=_cost)) if tenant_budget else None)
    return gw, prov


def gateway_settings():
    env = Env({"GEMINI_API_KEY": "g" * 20, "GATEWAY_SERVICE_KEYS": f"ticket-agent={KEY},knowledge-agent={KEY}x"})
    s = gateway_service.Settings(env)
    return s


@pytest.fixture
def gateway_server():
    started = []

    def start(**kw):
        gw, prov = make_gateway(**kw)
        app, lc, _ = gateway_service.create_app(gateway_settings(), gw=gw, embedder=FakeEmbedder())
        srv = ServerThread(app, free_port()).start()
        started.append(srv)
        return srv, gw, prov, lc

    yield start
    for s in started:
        s.stop()


def client_for(srv, service="ticket-agent", key=KEY):
    return RemoteGatewayProvider(srv.url, service, Secret(key))


# ---- gateway service ----------------------------------------------------------------------------
def test_gateway_requires_a_known_service_and_its_key(gateway_server):
    srv, *_ = gateway_server()
    body = {"messages": [{"role": "user", "content": "hi"}]}
    for headers in ({}, {"X-Service": "ticket-agent"}, {"X-Service": "ticket-agent", "X-Service-Key": "wrong" * 5},
                    {"X-Service": "intruder", "X-Service-Key": KEY}):
        assert httpx.post(f"{srv.url}/v1/chat", json=body, headers=headers).status_code == 401
    assert httpx.get(f"{srv.url}/v1/status").status_code == 401
    assert httpx.post(f"{srv.url}/v1/chat", content=b"not json", headers={"X-Service": "ticket-agent", "X-Service-Key": KEY}).status_code == 400


def test_remote_provider_round_trip_keeps_tool_calls_and_signatures(gateway_server):
    srv, gw, prov, _ = gateway_server()
    remote = client_for(srv)

    def lookup(x: str) -> str:
        """Look something up."""
        return "r"

    with request_context(RequestContext(user="sam", tenant="acme:abc", correlation_id="corr-1")):
        text = remote.chat([Message.user("hi")])
        call = remote.chat([Message.user("find")], tools=[Tool.from_function(lookup)])
    assert text.content == "hello" and text.usage.total_tokens == 10
    assert call.tool_calls[0].name == "lookup" and call.tool_calls[0].provider_data == b"sig"
    assert prov.seen[1]["tools"] == ["lookup"]                        # the schema reached the model
    rec = gw.audit.records[0]
    assert rec.tenant == "acme:abc" and rec.correlation_id == "corr-1"   # identity reached the gateway's audit
    assert remote.audit.records[-1].correlation_id == "corr-1" and remote.audit.records[-1].route == "scripted-light"
    assert [(r.name, r.tier) for r in remote.routes] == [("scripted-light", LIGHT)]


@pytest.mark.asyncio
async def test_async_path_and_unsupported_features(gateway_server):
    srv, *_ = gateway_server()
    remote = client_for(srv)
    assert (await remote.achat([Message.user("hi")])).content == "hello"
    with pytest.raises(ProviderException):
        remote.stream([Message.user("hi")])
    with pytest.raises(ProviderException):
        remote.chat([Message.user("hi")], response_model=dict)


def test_failures_keep_their_meaning_across_the_wire(gateway_server):
    srv, *_ = gateway_server(fail=True)
    with pytest.raises(GatewayExhausted):
        client_for(srv).chat([Message.user("hi")])
    srv2, *_ = gateway_server(tenant_budget=0.5)
    remote = client_for(srv2)
    with request_context(RequestContext(tenant="acme", correlation_id="b1")):
        remote.chat([Message.user("one")])                              # crosses the cap
        with pytest.raises(CostLimitException):
            remote.chat([Message.user("two")])


def test_wrong_service_key_is_a_clean_provider_error_not_a_crash(gateway_server):
    srv, *_ = gateway_server()
    with pytest.raises(ProviderException, match="rejected"):
        client_for(srv, key="nope" * 6).chat([Message.user("hi")])


def test_gateway_down_is_a_provider_error():
    remote = RemoteGatewayProvider(f"http://127.0.0.1:{free_port()}", "ticket-agent", Secret(KEY), timeout=2)
    with pytest.raises(ProviderException, match="unreachable"):
        remote.chat([Message.user("hi")])


def test_embeddings_go_through_the_gateway(gateway_server):
    srv, *_ = gateway_server()
    emb = RemoteEmbeddingProvider(srv.url, "knowledge-agent", Secret(KEY + "x"), batch=2)
    assert emb.embed(["a", "bb", "ccc"]) == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]      # three texts, two batches
    with pytest.raises(ProviderException):
        RemoteEmbeddingProvider(srv.url, "knowledge-agent", Secret("wrong" * 5)).embed(["a"])


def test_gateway_readiness_reflects_breakers_and_audit_is_bounded(gateway_server):
    srv, gw, prov, lc = gateway_server(fail=True)
    assert httpx.get(f"{srv.url}/readyz").status_code == 200
    with pytest.raises(GatewayExhausted):
        client_for(srv).chat([Message.user("hi")])                       # threshold 1: the breaker opens
    lc._cache.clear()
    r = httpx.get(f"{srv.url}/readyz")
    assert r.status_code == 503 and "breaker" in r.json()["checks"]["routes"]
    assert httpx.get(f"{srv.url}/healthz").status_code == 200            # unready is not dead
    status = httpx.get(f"{srv.url}/v1/status", headers={"X-Service": "ticket-agent", "X-Service-Key": KEY}).json()
    assert status["routes"][0]["breaker"] == "open"


def test_gateway_settings_reject_weak_service_keys():
    from deploylab.config import ConfigError
    with pytest.raises(ConfigError):
        gateway_service.Settings(Env({"GEMINI_API_KEY": "g" * 20, "GATEWAY_SERVICE_KEYS": "ticket-agent=short"}))


# ---- identity service ---------------------------------------------------------------------------
@pytest.fixture
def identity_server():
    common, idp = identity_service.build(Env({"TOKEN_SECRET": SECRET}))
    app, lc = identity_service.create_app(common, idp)
    srv = ServerThread(app, free_port()).start()
    yield srv
    srv.stop()


def test_login_and_exchange_shrink_access_over_http(identity_server):
    from identity.tokens import verify
    remote = RemoteIdentity(identity_server.url)
    token = remote.login("erin", "ticket-agent")                         # erin is exec + employee
    assert verify(SECRET.encode(), token, "ticket-agent").groups == ("employee", "exec")
    exchanged = remote.exchange(token, "ticket-agent", "knowledge-agent")
    claims = verify(SECRET.encode(), exchanged, "knowledge-agent")
    assert claims.groups == ("employee",) and claims.act == "ticket-agent"     # exec was not lent: the ceiling applied


def test_identity_rejects_unknown_users_and_bad_tokens(identity_server):
    remote = RemoteIdentity(identity_server.url)
    with pytest.raises(AuthError) as e:
        remote.login("mallory", "ticket-agent")
    assert e.value.reason == "unknown_user"
    forged = issue(b"attacker" * 6, "erin", ["exec"], "ticket-agent")
    with pytest.raises(AuthError) as e:
        remote.exchange(forged, "ticket-agent", "knowledge-agent")
    assert e.value.reason == "bad_signature"
    wrong_aud = remote.login("sam", "knowledge-agent")
    with pytest.raises(AuthError) as e:
        remote.exchange(wrong_aud, "ticket-agent", "knowledge-agent")
    assert e.value.reason == "wrong_audience"


def test_identity_unavailable_is_its_own_reason():
    with pytest.raises(AuthError) as e:
        RemoteIdentity(f"http://127.0.0.1:{free_port()}", timeout=1).login("sam", "ticket-agent")
    assert e.value.reason == "identity_unavailable"


def test_identity_refuses_to_start_with_a_short_secret():
    from deploylab.config import ConfigError
    with pytest.raises(ConfigError):
        identity_service.build(Env({"TOKEN_SECRET": "short"}))


# ---- ticket service -----------------------------------------------------------------------------
class FakePlatform:
    def __init__(self, delay=0.0):
        self.delay, self.calls, self.closed = delay, [], False

    def handle(self, ticket, operator="system", operator_token=None, correlation_id=""):
        time.sleep(self.delay)
        self.calls.append((ticket, operator, operator_token, correlation_id))
        return Outcome(ticket.id, ticket.tenant, "released", reply="done", correlation_id=correlation_id,
                       triage=TriageResult(category="billing", severity="high", customer_id="C-1002", summary="double charge"),
                       tools_used=["lookup_order"], model_calls=4)

    def close(self):
        self.closed = True


def ticket_env(**extra):
    base = {"TOKEN_SECRET": SECRET, "GATEWAY_URL": "http://127.0.0.1:1", "GATEWAY_SERVICE_KEY": KEY,
            "KNOWLEDGE_URL": "http://127.0.0.1:1", "IDENTITY_URL": "http://127.0.0.1:1"}
    base.update(extra)
    return Env(base)


@pytest.fixture
def ticket_server():
    started = []

    def start(platform=None, **extra):
        platform = platform or FakePlatform()
        app, lc, _ = ticket_service.create_app(ticket_service.Settings(ticket_env(**extra)), platform=platform)
        srv = ServerThread(app, free_port()).start()
        started.append(srv)
        return srv, platform, lc

    yield start
    for s in started:
        s.stop()


def op_token(user="sam", aud="ticket-agent", ttl=60):
    return issue(SECRET.encode(), user, ["employee", "support"], aud, ttl_s=ttl)


def test_ticket_service_authenticates_every_request(ticket_server):
    srv, platform, _ = ticket_server()
    body = {"text": "refund please"}
    assert httpx.post(f"{srv.url}/tickets", json=body).json()["reason"] == "missing_token"
    forged = issue(b"x" * 40, "sam", ["employee"], "ticket-agent")
    assert httpx.post(f"{srv.url}/tickets", json=body, headers={"Authorization": f"Bearer {forged}"}).json()["reason"] == "bad_signature"
    wrong = op_token(aud="knowledge-agent")
    assert httpx.post(f"{srv.url}/tickets", json=body, headers={"Authorization": f"Bearer {wrong}"}).json()["reason"] == "wrong_audience"
    expired = op_token(ttl=-5)
    assert httpx.post(f"{srv.url}/tickets", json=body, headers={"Authorization": f"Bearer {expired}"}).json()["reason"] == "expired"
    assert platform.calls == []                                            # none of the four reached the platform


def test_operator_comes_from_the_token_not_the_body(ticket_server):
    srv, platform, _ = ticket_server()
    token = op_token("sam")
    r = httpx.post(f"{srv.url}/tickets", json={"id": "T9", "text": "hello", "operator": "erin", "tenant": "evil"},
                   headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": "c-42"})
    assert r.status_code == 200 and r.json()["action"] == "released" and r.headers["x-correlation-id"] == "c-42"
    assert r.json()["triage"]["category"] == "billing" and r.json()["tools_used"] == ["lookup_order"]   # the real outcome shape
    ticket, operator, passed_token, corr = platform.calls[0]
    assert operator == "sam" and ticket.tenant == "acme" and passed_token == token and corr == "c-42"


def test_bad_bodies_are_400_not_500(ticket_server):
    srv, _, _ = ticket_server()
    h = {"Authorization": f"Bearer {op_token()}"}
    assert httpx.post(f"{srv.url}/tickets", content=b"nope", headers=h).status_code == 400
    assert httpx.post(f"{srv.url}/tickets", json={"text": "  "}, headers=h).status_code == 400
    assert httpx.post(f"{srv.url}/tickets", json={"text": "x" * 5000}, headers=h).status_code == 400


def test_overload_is_refused_with_retry_after_instead_of_queueing_forever(ticket_server):
    import concurrent.futures as cf
    srv, platform, _ = ticket_server(platform=FakePlatform(delay=0.6), MAX_CONCURRENT_TICKETS="1", MAX_WAITING_TICKETS="1")
    h = {"Authorization": f"Bearer {op_token()}"}
    with cf.ThreadPoolExecutor(5) as pool:
        futs = [pool.submit(httpx.post, f"{srv.url}/tickets", json={"text": f"t{i}"}, headers=h, timeout=20) for i in range(5)]
        codes = sorted(f.result().status_code for f in futs)
    assert codes.count(200) >= 2 and 429 in codes, codes
    refused = [f.result() for f in futs if f.result().status_code == 429][0]
    assert refused.headers["retry-after"] == "5"


def test_draining_refuses_new_tickets_and_readiness_names_the_hard_dependencies(ticket_server):
    srv, _, lc = ticket_server()
    r = httpx.get(f"{srv.url}/readyz")
    assert r.status_code == 503 and "gateway" in r.json()["checks"] and "identity" in r.json()["checks"]     # nothing is listening on :1
    assert r.json()["checks"]["soft:knowledge"].startswith("unavailable") or "not ready" in r.json()["checks"]["soft:knowledge"]
    lc.draining = True
    assert httpx.post(f"{srv.url}/tickets", json={"text": "x"}, headers={"Authorization": f"Bearer {op_token()}"}).status_code == 503


# ---- knowledge service --------------------------------------------------------------------------
class StubAssistant:
    def ask(self, user, question, corr=""):
        raise AssertionError("the model must not be reached by an unauthenticated call")


def test_knowledge_service_probes_public_card_public_everything_else_authenticated():
    port = free_port()
    env = Env({"TOKEN_SECRET": SECRET, "GATEWAY_URL": "http://127.0.0.1:1", "GATEWAY_SERVICE_KEY": KEY,
               "PORT": str(port), "PUBLIC_URL": "https://knowledge.internal.example"})
    app, lc = knowledge_service.create_app(knowledge_service.Settings(env), assistant=StubAssistant())
    srv = ServerThread(app, port).start()
    try:
        assert httpx.get(f"{srv.url}/healthz").status_code == 200
        assert httpx.get(f"{srv.url}/readyz").status_code == 200              # no index or gateway wired in this test
        card = httpx.get(f"{srv.url}/.well-known/agent-card.json").json()
        assert "bearer" in str(card).lower() and "knowledge.internal.example" in str(card)   # discoverable auth, configured URL
        assert httpx.post(f"{srv.url}/", json={}).status_code == 401           # JSON-RPC endpoint needs a token
        assert httpx.post(f"{srv.url}/", json={}, headers={"Authorization": "Bearer junk"}).status_code == 401
    finally:
        srv.stop()


def test_an_unexpected_error_is_json_with_the_correlation_id_and_no_detail(ticket_server):
    class Exploding(FakePlatform):
        def handle(self, *a, **k):
            raise RuntimeError("internal detail that must not reach the caller")

    srv, _, _ = ticket_server(platform=Exploding())
    r = httpx.post(f"{srv.url}/tickets", json={"text": "x"}, headers={"Authorization": f"Bearer {op_token()}", "X-Correlation-Id": "boom-1"})
    assert r.status_code == 500 and r.json() == {"error": "internal", "correlation_id": "boom-1"}
    assert "internal detail" not in r.text


def test_cache_is_only_available_to_callers_whose_answers_are_safe_to_replay(gateway_server):
    srv, gw, prov, _ = gateway_server()
    seen = []
    real = gw.achat

    async def spy(messages, **kw):
        seen.append(kw.get("no_cache", False))
        return await real(messages, **kw)

    gw.achat = spy
    body = {"messages": [{"role": "user", "content": "hi"}]}
    httpx.post(f"{srv.url}/v1/chat", json=body, headers={"X-Service": "ticket-agent", "X-Service-Key": KEY})
    httpx.post(f"{srv.url}/v1/chat", json=body, headers={"X-Service": "knowledge-agent", "X-Service-Key": KEY + "x"})
    assert seen == [True, False]                 # tickets: never cached; knowledge: cache allowed (its scope carries the access level)


def test_readiness_answers_from_the_last_result_and_does_not_wait_on_a_slow_dependency():
    import asyncio as aio
    import time as t
    from starlette.testclient import TestClient
    from deploylab.runtime import Lifecycle, make_app

    lc = Lifecycle("demo", "1")
    calls = {"n": 0}

    async def slow():
        calls["n"] += 1
        await aio.sleep(1.5)
        return True

    lc.checks["dep"] = slow
    c = TestClient(make_app(lc, []))
    assert c.get("/readyz").status_code == 200           # cold: the first evaluation waits once
    lc._cache["dep"] = (t.monotonic() - 10, "ok")         # now the result is stale
    t0 = t.perf_counter()
    assert c.get("/readyz").status_code == 200
    assert t.perf_counter() - t0 < 0.5, "a stale result must be served immediately while a refresh runs"
