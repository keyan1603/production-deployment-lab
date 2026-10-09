"""Offline tests: configuration, wire format, probes, logging. No network, no API keys."""

import asyncio
import json
import logging

import pytest
from requisite.core.interfaces import ChatResponse, Message, Role, ToolCall, Usage
from requisite.tools.base import Tool
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from deploylab import logs, serde
from deploylab.config import Common, ConfigError, Env, Secret
from deploylab.runtime import Lifecycle, make_app


# ---- configuration ------------------------------------------------------------------------------
def test_missing_settings_are_reported_together():
    env = Env({})
    env.secret("TOKEN_SECRET", min_length=32)
    env.str("GATEWAY_URL", required=True)
    env.secret("GATEWAY_SERVICE_KEY")
    with pytest.raises(ConfigError) as exc:
        env.done()
    assert len(exc.value.problems) == 3                      # all three at once, not one per restart
    assert "TOKEN_SECRET (or TOKEN_SECRET_FILE) is required" in str(exc.value)


def test_secret_from_file_wins_and_never_prints(tmp_path):
    f = tmp_path / "token"
    f.write_text("from-the-file-" + "x" * 30 + "\n")
    env = Env({"TOKEN_SECRET": "from-the-env", "TOKEN_SECRET_FILE": str(f)})
    s = env.secret("TOKEN_SECRET", min_length=32)
    env.done()
    assert s.reveal().startswith("from-the-file-") and not s.reveal().endswith("\n")
    assert "from-the-file" not in repr(s) and "from-the-file" not in str(s) and repr(s) == "Secret(***)"


def test_unreadable_secret_file_and_short_secret_are_config_errors(tmp_path):
    env = Env({"A_FILE": str(tmp_path / "nope"), "B": "short"})
    env.secret("A")
    env.secret("B", min_length=32)
    with pytest.raises(ConfigError) as exc:
        env.done()
    assert any("A_FILE" in p for p in exc.value.problems) and any("at least 32" in p for p in exc.value.problems)


def test_port_range_and_number_validation():
    env = Env({"PORT": "99999", "DRAIN_SECONDS": "soon"})
    Common(env, "x", 8000)
    with pytest.raises(ConfigError) as exc:
        env.done()
    assert len(exc.value.problems) == 2


def test_defaults_work_without_any_environment():
    env = Env({})
    c = Common(env, "demo", 8123)
    env.done()
    assert (c.port, c.host, c.public_url) == (8123, "127.0.0.1", "http://127.0.0.1:8123")


# ---- wire format --------------------------------------------------------------------------------
def test_messages_round_trip_including_provider_bytes():
    call = ToolCall(id="c1", name="lookup_order", arguments={"order_id": "O-1"}, provider_data={"thought_signature": b"\x00\xffsig"})
    msgs = [Message.system("be brief"), Message.user("hi"), Message.assistant_tool_calls([call]),
            Message.tool_result("shipped", tool_call_id="c1", name="lookup_order")]
    wire = json.loads(json.dumps(serde.encode_messages(msgs)))      # prove it survives real JSON
    back = serde.decode_messages(wire)
    assert [m.role for m in back] == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL]
    assert back[2].tool_calls[0].provider_data == {"thought_signature": b"\x00\xffsig"}
    assert back[3].tool_call_id == "c1"


def test_tool_schema_travels_but_the_gateway_cannot_run_it():
    def lookup_order(order_id: str) -> str:
        """Find an order."""
        return "x"

    tool = Tool.from_function(lookup_order)
    [decoded] = serde.decode_tools(json.loads(json.dumps(serde.encode_tools([tool]))))
    assert decoded.name == "lookup_order" and decoded.parameters_schema == tool.parameters_schema
    with pytest.raises(Exception):
        decoded.execute(order_id="O-1")


def test_response_round_trip():
    resp = ChatResponse(content="ok", model="m", provider="p", usage=Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
                        finish_reason="stop", tool_calls=[ToolCall(id="1", name="t", arguments={"a": 1}, provider_data=b"raw")])
    back = serde.decode_response(json.loads(json.dumps(serde.encode_response(resp))))
    assert back.content == "ok" and back.usage.total_tokens == 7 and back.tool_calls[0].provider_data == b"raw"


# ---- probes and lifecycle -----------------------------------------------------------------------
def _app(lc):
    async def hello(_):
        return JSONResponse({"hi": 1})
    return make_app(lc, [Route("/hello", hello)])


def test_healthz_ignores_dependencies_but_readyz_does_not():
    lc = Lifecycle("demo", "1.0")
    lc.checks["db"] = lambda: "connection refused"
    c = TestClient(_app(lc))
    assert c.get("/healthz").status_code == 200                      # alive, even though a dependency is down
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["db"] == "connection refused"
    lc._cache.clear()
    lc.checks["db"] = lambda: True
    assert c.get("/readyz").status_code == 200


def test_soft_dependency_is_reported_but_does_not_gate_readiness():
    lc = Lifecycle("demo", "1.0")
    lc.soft_checks["knowledge"] = lambda: "unavailable: ConnectError"
    r = TestClient(_app(lc)).get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["soft:knowledge"].startswith("unavailable")


def test_async_check_and_check_exception_become_unready_not_a_crash():
    lc = Lifecycle("demo", "1.0")

    async def slow_ok():
        await asyncio.sleep(0)
        return True

    def boom():
        raise RuntimeError("secret detail must not leak")

    lc.checks["a"], lc.checks["b"] = slow_ok, boom
    r = TestClient(_app(lc)).get("/readyz")
    assert r.status_code == 503 and r.json()["checks"]["a"] == "ok" and r.json()["checks"]["b"] == "unavailable: RuntimeError"
    assert "secret detail" not in r.text


def test_background_startup_task_gates_readiness_until_done():
    import threading
    gate = threading.Event()
    lc = Lifecycle("demo", "1.0")
    lc.run_in_background("index", gate.wait)
    c = TestClient(_app(lc))
    assert c.get("/readyz").status_code == 503 and c.get("/readyz").json()["checks"]["index"] == "pending"
    gate.set()
    for _ in range(100):
        if lc.background["index"] == "ok":
            break
        import time; time.sleep(0.01)
    assert c.get("/readyz").status_code == 200


def test_failed_startup_task_keeps_the_service_unready_and_says_why():
    lc = Lifecycle("demo", "1.0")
    lc.run_in_background("index", lambda: (_ for _ in ()).throw(ValueError("corpus missing")), attempts=2, delay_s=0.05)
    import time; time.sleep(0.5)
    r = TestClient(_app(lc)).get("/readyz")
    assert r.status_code == 503 and "failed after 2 attempts: ValueError: corpus missing" in r.json()["checks"]["index"]


def test_startup_task_retries_until_its_dependency_appears():
    import time
    lc, attempts = Lifecycle("demo", "1.0"), {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("gateway not up yet")

    lc.run_in_background("index", flaky, attempts=10, delay_s=0.05)
    time.sleep(0.05)
    assert lc.background["index"].startswith("pending")
    for _ in range(100):
        if lc.background["index"] == "ok":
            break
        time.sleep(0.02)
    assert lc.background["index"] == "ok" and attempts["n"] == 3


def test_draining_fails_readiness_but_keeps_serving_requests():
    lc = Lifecycle("demo", "1.0")
    c = TestClient(_app(lc))
    lc.draining = True
    assert c.get("/readyz").status_code == 503 and c.get("/readyz").json()["status"] == "draining"
    assert c.get("/hello").status_code == 200                        # still serving while the balancer notices
    assert c.get("/healthz").status_code == 200


def test_correlation_id_is_echoed_and_generated():
    c = TestClient(_app(Lifecycle("demo", "1.0")))
    assert c.get("/hello", headers={"X-Correlation-Id": "abc-1"}).headers["x-correlation-id"] == "abc-1"
    generated = c.get("/hello").headers["x-correlation-id"]
    assert len(generated) == 12 and c.get("/hello").headers["x-service"] == "demo"


# ---- logging ------------------------------------------------------------------------------------
def test_logs_are_json_with_correlation_id_and_tokens_scrubbed(capsys):
    logs.setup("demo", "INFO")
    log = logging.getLogger("t")
    tok = logs.correlation_id.set("corr-9")
    try:
        logs.event(log, "calling with Authorization: Bearer abcdefghijklmnop1234", header="Bearer zzzzzzzzzzzzzzzz9999", key="AIza" + "a" * 30)
    finally:
        logs.correlation_id.reset(tok)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["correlation_id"] == "corr-9" and line["service"] == "demo"
    text = json.dumps(line)
    assert "abcdefghijklmnop1234" not in text and "zzzzzzzzzzzzzzzz9999" not in text and "a" * 30 not in text
    assert "Bearer ***" in line["msg"]
