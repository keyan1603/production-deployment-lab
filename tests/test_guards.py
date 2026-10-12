"""Offline tests: quota shedding with a priority reserve, the daily spend cap, persistence, and the ticket freeze switch."""

import datetime as dt
import json

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from requisite.core.context import RequestContext, request_context
from requisite.core.exceptions import CostLimitException
from requisite.core.interfaces import Message

from a2a_layer.server import ServerThread, free_port
from deploylab import telemetry as tele
from deploylab.config import Env, Secret
from deploylab.guards import QuotaGuard, SpendGuard, day_key
from deploylab.remote import RemoteGatewayProvider
from gateway.provider import GatewayExhausted
from services import gateway_service, ticket_service
from tests.test_services import FakeEmbedder, FixedPlatform, KEY, make_gateway, op_token, send_many, ticket_env


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at

    def advance(self, **kw):
        self.at += dt.timedelta(**kw)


T0 = dt.datetime(2026, 10, 12, 10, 0, tzinfo=dt.timezone.utc)


# ---- QuotaGuard ---------------------------------------------------------------------------------
def test_ratio_and_the_per_caller_thresholds_make_a_priority_reserve():
    g = QuotaGuard({"light": 100}, {"tickets": 0.90, "knowledge": 0.98}, clock=Clock(T0))
    g.record("light", 89)
    assert g.check("tickets", ["light"]) is None and g.check("knowledge", ["light"]) is None
    g.record("light", 2)                                           # 91 percent: tickets are shed, knowledge keeps working
    assert "tickets is shed at 90%" in g.check("tickets", ["light"]) and g.check("knowledge", ["light"]) is None
    g.record("light", 7)                                           # 98 percent
    assert g.check("knowledge", ["light"]) is not None
    assert g.check("monitor", ["light"]) is None                   # no threshold set means 100 percent: the probes run to the end
    g.record("light", 2)
    assert g.check("monitor", ["light"]) is not None


def test_a_caller_is_only_shed_when_every_model_that_could_serve_it_is_full():
    g = QuotaGuard({"light": 100, "heavy": 100}, {"tickets": 0.9}, clock=Clock(T0))
    g.record("light", 95)
    assert g.check("tickets", ["light", "heavy"]) is None          # failover to heavy is still possible
    g.record("heavy", 95)
    assert g.check("tickets", ["light", "heavy"]) is not None


def test_models_without_a_known_limit_never_cause_shedding():
    g = QuotaGuard({"light": 10}, clock=Clock(T0))
    assert g.check("x", ["local"]) is None and g.ratio("local") == 0.0


def test_counts_survive_a_restart_and_reset_at_the_provider_day(tmp_path):
    clock, path = Clock(T0), tmp_path / "quota.json"
    QuotaGuard({"light": 100}, path=path, clock=clock).record("light", 40)
    again = QuotaGuard({"light": 100}, path=path, clock=clock)
    assert again.used("light") == 40                               # a restart must not hand back the day's allowance
    clock.advance(hours=15)                                       # past midnight UTC
    assert again.used("light") == 0 and QuotaGuard({"light": 100}, path=path, clock=clock).used("light") == 0


def test_the_providers_day_can_start_at_any_hour():
    assert day_key(dt.datetime(2026, 10, 12, 6, 59, tzinfo=dt.timezone.utc), 7) == "2026-10-11"
    assert day_key(dt.datetime(2026, 10, 12, 7, 0, tzinfo=dt.timezone.utc), 7) == "2026-10-12"


def test_an_unreadable_state_file_never_blocks_traffic(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{not json")
    g = QuotaGuard({"light": 10}, path=path, clock=Clock(T0))
    assert g.used("light") == 0 and g.check("x", ["light"]) is None


# ---- SpendGuard ---------------------------------------------------------------------------------
def test_the_daily_budget_stops_callers_but_not_the_exempt_one(tmp_path):
    clock = Clock(T0)
    s = SpendGuard(1.00, exempt=("monitor",), path=tmp_path / "spend.json", clock=clock)
    s.record(0.60)
    assert s.check("tickets") is None and s.ratio() == pytest.approx(0.6)
    s.record(0.45)
    assert "daily budget of $1 is spent" in s.check("tickets") and s.check("monitor") is None
    assert SpendGuard(1.00, path=tmp_path / "spend.json", clock=clock).spent() == pytest.approx(1.05)
    clock.advance(days=1)
    assert s.check("tickets") is None and s.spent() == 0


# ---- through the gateway service ----------------------------------------------------------------
@pytest.fixture
def tel():
    reader = InMemoryMetricReader()
    t = tele.setup("gateway", "v1", span_exporter=InMemorySpanExporter(), metric_reader=reader, set_global=False)
    previous, tele._current = tele._current, t
    t.reader = reader
    yield t
    tele._current = previous


def gateway_with(**env):
    base = {"GEMINI_API_KEY": "g" * 20, "GATEWAY_SERVICE_KEYS": f"ticket-agent={KEY},knowledge-agent={KEY}x,monitor-agent={KEY}y"}
    base.update(env)
    gw, prov = make_gateway()
    app, lc, _ = gateway_service.create_app(gateway_service.Settings(Env(base)), gw=gw, embedder=FakeEmbedder())
    srv = ServerThread(app, free_port()).start()
    return srv, prov


def client(srv, service="ticket-agent", suffix=""):
    return RemoteGatewayProvider(srv.url, service, Secret(KEY + suffix))


def ask(remote, n=1):
    with request_context(RequestContext(tenant="acme", correlation_id=f"g-{n}")):
        return remote.chat([Message.user(f"hello {n}")])


def test_the_gateway_sheds_the_low_priority_caller_first_and_says_why(tel, tmp_path):
    srv, _ = gateway_with(QUOTA_RPD="scripted=10", QUOTA_SHED="ticket-agent=0.5,knowledge-agent=0.9", STATE_DIR=str(tmp_path))
    try:
        tickets, knowledge = client(srv), client(srv, "knowledge-agent", "x")
        for i in range(5):
            ask(tickets, i)                                        # 5 of 10 requests: the tickets' threshold
        with pytest.raises(GatewayExhausted, match="shed at 50%"):
            ask(tickets, 99)
        assert ask(knowledge, 100).content == "hello"              # the reserve: knowledge still gets through
        r = httpx.post(f"{srv.url}/v1/chat", json={"messages": [{"role": "user", "content": "x"}]},
                       headers={"X-Service": "ticket-agent", "X-Service-Key": KEY})
        assert r.status_code == 503 and r.headers["retry-after"] == "60" and r.json()["error"] == "quota_guard"
        status = httpx.get(f"{srv.url}/v1/status", headers={"X-Service": "ticket-agent", "X-Service-Key": KEY}).json()
        assert status["quota"]["scripted"]["used"] == 6 and status["quota"]["scripted"]["ratio"] == 0.6
    finally:
        srv.stop()
    shed = tel.reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
    assert any(m.name == "ai.gateway.shed" for m in shed)
    assert json.loads((tmp_path / "quota.json").read_text())["data"]["scripted"] == 6          # persisted


def test_a_request_the_provider_refused_still_counted_against_the_allowance(tel):
    srv, prov = gateway_with(QUOTA_RPD="scripted=10")
    try:
        prov.fail = True
        with pytest.raises(GatewayExhausted):
            ask(client(srv), 1)
        status = httpx.get(f"{srv.url}/v1/status", headers={"X-Service": "ticket-agent", "X-Service-Key": KEY}).json()
        assert status["quota"]["scripted"]["used"] == 1
    finally:
        srv.stop()


def test_the_spend_cap_refuses_callers_with_a_budget_error_and_exempts_the_monitor(tel):
    srv, _ = gateway_with(DAILY_BUDGET_USD="0.5", BUDGET_EXEMPT="monitor-agent")          # the scripted route costs 1.0 per call
    try:
        tickets = client(srv)
        ask(tickets, 1)                                            # spends 1.0, over the 0.5 cap
        with pytest.raises(CostLimitException, match="daily budget"):
            ask(tickets, 2)
        assert ask(client(srv, "monitor-agent", "y"), 3).content == "hello"
        assert httpx.get(f"{srv.url}/v1/status", headers={"X-Service": "ticket-agent", "X-Service-Key": KEY}).json()["spend"]["budget"] == 0.5
    finally:
        srv.stop()


def test_a_malformed_guard_setting_is_a_startup_error():
    from deploylab.config import ConfigError
    with pytest.raises(ConfigError, match="QUOTA_RPD"):
        gateway_service.Settings(Env({"GEMINI_API_KEY": "g" * 20, "GATEWAY_SERVICE_KEYS": f"ticket-agent={KEY}", "QUOTA_RPD": "light=lots"}))


# ---- the freeze switch --------------------------------------------------------------------------
def test_freeze_stops_new_tickets_and_unfreeze_restores_them():
    platform = FixedPlatform()
    app, lc, rs = ticket_service.create_app(ticket_service.Settings(ticket_env(ADMIN_KEY="a" * 20)), platform=platform)
    srv = ServerThread(app, free_port()).start()
    try:
        h = {"X-Admin-Key": "a" * 20}
        assert httpx.get(f"{srv.url}/admin/freeze").status_code == 401
        assert httpx.get(f"{srv.url}/admin/freeze", headers={"X-Admin-Key": "wrong" * 5}).status_code == 401
        assert send_many(srv, 1)[0]["action"] == "released"
        assert httpx.post(f"{srv.url}/admin/freeze", json={"on": True, "reason": "cost runaway"}, headers=h).json()["on"] is True
        r = httpx.post(f"{srv.url}/tickets", json={"text": "x"}, headers={"Authorization": f"Bearer {op_token()}"})
        assert r.status_code == 503 and r.json() == {"error": "frozen", "reason": "cost runaway"} and r.headers["retry-after"] == "300"
        assert httpx.post(f"{srv.url}/admin/freeze", json={"on": False}, headers=h).json()["on"] is False
        assert send_many(srv, 1, prefix="U")[0]["action"] == "released"
    finally:
        srv.stop()


def test_force_tier_accepts_only_known_tiers():
    from deploylab.config import ConfigError
    base = {"GEMINI_API_KEY": "g" * 20, "GATEWAY_SERVICE_KEYS": f"ticket-agent={KEY}"}
    assert gateway_service.Settings(Env({**base, "FORCE_TIER": "heavy"})).force_tier == "heavy"
    assert gateway_service.Settings(Env(base)).force_tier == ""
    with pytest.raises(ConfigError, match="FORCE_TIER"):
        gateway_service.Settings(Env({**base, "FORCE_TIER": "turbo"}))
