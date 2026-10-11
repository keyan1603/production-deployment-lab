"""Offline tests for telemetry: trace continuation, bounded metric labels, log correlation, domain metrics."""

import json
import logging

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from requisite.core.context import RequestContext, request_context
from requisite.core.interfaces import Message
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_layer.server import ServerThread, free_port
from deploylab import logs, telemetry as tele
from deploylab.config import Secret
from deploylab.remote import RemoteGatewayProvider
from deploylab.runtime import Lifecycle, make_app
from tests.test_services import (FixedPlatform, KEY, gateway_settings, make_gateway, op_token, release_of, send_many, ticket_env)
from services import gateway_service, ticket_service
from tests.test_services import FakeEmbedder

TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN}-01"


@pytest.fixture
def tel():
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    t = tele.setup("demo", "v1", span_exporter=spans, metric_reader=reader, set_global=False)
    previous = tele._current
    tele._current = t
    t.spans, t.reader = spans, reader
    yield t
    tele._current = previous
    t.tracer_provider.force_flush()


def points(tel, name):
    """{frozenset(attrs): value} for one metric, summing counters and taking histogram counts."""
    out = {}
    data = tel.reader.get_metrics_data()
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name != name:
                    continue
                for p in m.data.data_points:
                    value = getattr(p, "value", None)
                    if value is None:
                        value = p.count
                    out[frozenset(p.attributes.items())] = value
    return out


def app(lc=None):
    async def tickets(_):
        logging.getLogger("t").info("inside the handler")
        return JSONResponse({"ok": 1})

    async def boom(_):
        raise RuntimeError("bug")

    return make_app(lc or Lifecycle("demo", "v1"), [Route("/tickets", tickets), Route("/v1/chat", boom), Route("/anything/{x}", tickets)])


def finished(tel):
    tel.tracer_provider.force_flush()
    return tel.spans.get_finished_spans()


# ---- traces -------------------------------------------------------------------------------------
def test_a_server_span_continues_the_callers_trace(tel):
    TestClient(app()).get("/tickets", headers={"traceparent": TRACEPARENT, "X-Correlation-Id": "c-1"})
    [span] = [s for s in finished(tel) if s.name == "GET /tickets"]
    assert f"{span.context.trace_id:032x}" == TRACE_ID and f"{span.parent.span_id:016x}" == PARENT_SPAN
    assert span.attributes["app.correlation_id"] == "c-1" and span.attributes["http.response.status_code"] == 200
    assert span.kind == trace.SpanKind.SERVER


def test_without_a_traceparent_a_new_trace_starts(tel):
    TestClient(app()).get("/tickets")
    [span] = finished(tel)
    assert span.parent is None and span.context.trace_id != 0


def test_an_unhandled_error_marks_the_span_and_the_5xx_counter(tel):
    r = TestClient(app(), raise_server_exceptions=False).get("/v1/chat")
    assert r.status_code == 500
    [span] = [s for s in finished(tel) if s.name == "GET /v1/chat"]
    assert span.status.status_code == trace.StatusCode.ERROR and any(e.name == "exception" for e in span.events)
    assert points(tel, "http.server.requests")[frozenset({("method", "GET"), ("path", "/v1/chat"), ("status_class", "5xx")})] == 1


def test_probes_are_not_traced_or_counted(tel):
    c = TestClient(app())
    c.get("/healthz"), c.get("/readyz")
    assert finished(tel) == () and points(tel, "http.server.requests") == {}


# ---- metrics ------------------------------------------------------------------------------------
def test_request_metrics_use_a_bounded_set_of_labels(tel):
    c = TestClient(app())
    for i in range(20):
        c.get(f"/anything/{i}")                                # 20 different ids must be ONE label value, not 20 series
    c.get("/tickets")
    paths = {dict(k)["path"] for k in points(tel, "http.server.requests")}
    assert paths == {"other", "/tickets"}
    assert tele.path_label("/admin/releases/canary") == "/admin/releases" and tele.path_label("/login") == "/login"


def test_duration_is_a_histogram_in_seconds(tel):
    TestClient(app()).get("/tickets")
    assert sum(points(tel, "http.server.duration").values()) == 1
    assert tele.DURATION_BUCKETS[0] < 1 and tele.DURATION_BUCKETS[-1] >= 300


# ---- logs ---------------------------------------------------------------------------------------
def test_log_lines_inside_a_request_carry_the_trace_id(tel, capsys):
    logs.setup("demo", "INFO")
    TestClient(app()).get("/tickets", headers={"traceparent": TRACEPARENT})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    inside = next(line for line in lines if line["msg"] == "inside the handler")
    assert inside["trace_id"] == TRACE_ID and len(inside["span_id"]) == 16
    outside = logging.getLogger("t")
    outside.info("after the request")
    assert "trace_id" not in json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# ---- propagation to the next service ------------------------------------------------------------
def test_the_remote_gateway_client_forwards_the_trace(tel):
    received = {}

    async def chat(request):
        received.update(request.headers)
        return JSONResponse({"error": "x"}, status_code=503)

    srv = ServerThread(make_app(Lifecycle("sink", "1"), [Route("/v1/chat", chat, methods=["POST"])]), free_port()).start()
    try:
        remote = RemoteGatewayProvider(srv.url, "ticket-agent", Secret(KEY))
        with tel.tracer.start_as_current_span("ticket.handle") as span, request_context(RequestContext(correlation_id="c-9")):
            try:
                remote.chat([Message.user("hi")])
            except Exception:  # noqa: BLE001 - the sink answers 503 on purpose
                pass
        assert received["traceparent"].split("-")[1] == f"{span.get_span_context().trace_id:032x}"
        assert received["x-correlation-id"] == "c-9"
    finally:
        srv.stop()


# ---- domain metrics -----------------------------------------------------------------------------
def test_ticket_outcomes_are_counted_by_release_and_role(tel):
    srv_threads = []
    platform = FixedPlatform(action="released")
    app_, lc, rs = ticket_service.create_app(ticket_service.Settings(ticket_env()), platform=platform)
    srv = ServerThread(app_, free_port()).start()
    try:
        rs.add(release_of("held-one"), FixedPlatform(action="held"))
        rs.set_canary("held-one", 50)
        send_many(srv, 8)
    finally:
        srv.stop()
    out = points(tel, "ai.ticket.outcomes")
    assert out[frozenset({("action", "released"), ("release", "test"), ("role", "control")})] >= 1
    assert out[frozenset({("action", "held"), ("release", "held-one"), ("role", "canary")})] >= 1
    holds = points(tel, "ai.ticket.holds")
    assert sum(holds.values()) == sum(v for k, v in out.items() if dict(k)["action"] == "held")
    assert points(tel, "ai.ticket.duration")


def test_gateway_requests_tokens_cost_and_breaker_state_are_measured(tel):
    gw, prov = make_gateway()
    app_, lc, _ = gateway_service.create_app(gateway_settings(), gw=gw, embedder=FakeEmbedder())
    srv = ServerThread(app_, free_port()).start()
    try:
        remote = RemoteGatewayProvider(srv.url, "ticket-agent", Secret(KEY))
        with request_context(RequestContext(tenant="acme", correlation_id="m-1")):
            remote.chat([Message.user("hi")])
        prov.fail = True
        with request_context(RequestContext(tenant="acme", correlation_id="m-2")):
            with pytest.raises(Exception):
                remote.chat([Message.user("again")])
    finally:
        srv.stop()
    requests = points(tel, "ai.gateway.requests")
    assert requests[frozenset({("caller", "ticket-agent"), ("route", "scripted-light"), ("tier", "light"), ("outcome", "ok")})] == 1
    assert any(dict(k)["outcome"] == "exhausted" for k in requests)
    tokens = points(tel, "ai.gateway.tokens")
    assert tokens[frozenset({("direction", "input"), ("route", "scripted-light")})] == 5
    assert points(tel, "ai.gateway.cost_usd")
    assert points(tel, "ai.gateway.attempt_failures")
    assert points(tel, "ai.gateway.breaker_state")[frozenset({("route", "scripted-light")})] == 2      # the failure opened it (threshold 1)
