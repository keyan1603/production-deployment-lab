"""Offline tests for the Gemini simulator: quota shapes, faults, and the scripted brain's behaviours."""

import base64
import json
import time

import pytest
from starlette.testclient import TestClient

from sim.gemini_sim import NOT_FOUND, Sim, create_app, embed

MODEL = "gemini-3.5-flash-lite"
URL = f"/v1beta/models/{MODEL}:generateContent"


def body(system, user, tools=None, extra=None):
    out = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}] + (extra or [])}
    if tools:
        out["tools"] = [{"functionDeclarations": [{"name": n} for n in tools]}]
    return out


@pytest.fixture
def client():
    sim = Sim(rpm=1000, rpd=1000)
    sim.latency_ms = {MODEL: 1.0, "gemini-3.5-flash": 1.0}
    c = TestClient(create_app(sim))
    c.sim = sim
    return c


def text_of(r):
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def test_triage_returns_json_for_the_ticket(client):
    r = client.post(URL, json=body("You triage customer support tickets. Reply with ONLY a JSON object", "Customer C-1002: order O-5002 was charged twice"))
    d = json.loads(text_of(r))
    assert (d["category"], d["severity"], d["customer_id"], d["order_id"]) == ("billing", "high", "C-1002", "O-5002")
    assert r.json()["usageMetadata"]["totalTokenCount"] > 0


def test_a_prompt_that_renames_the_categories_gets_the_new_names(client):
    system = 'You triage customer support tickets. keys: "category" (payments, tech or access), "severity" (low, medium or high)'
    d = json.loads(text_of(client.post(URL, json=body(system, "order O-5002 was charged twice"))))
    assert d["category"] == "payments"


def test_resolver_asks_for_facts_with_signatures_then_writes_the_note(client):
    first = client.post(URL, json=body("You are a support resolver.", "C-1002 O-5002 charged twice", tools=["get_customer", "get_order", "search_kb"])).json()
    calls = [p["functionCall"]["name"] for p in first["candidates"][0]["content"]["parts"]]
    assert calls == ["get_customer", "get_order", "search_kb"]
    assert base64.b64decode(first["candidates"][0]["content"]["parts"][0]["thoughtSignature"]) == b"sim-signature"
    results = [{"role": "user", "parts": [{"functionResponse": {"name": "get_order", "response": {"result": "status paid_twice"}}}]}]
    note = text_of(client.post(URL, json=body("You are a support resolver.", "C-1002", tools=["get_order"], extra=results)))
    assert "paid_twice" in note


SOURCES = ("Sources:\n[ENG-001] Rollback runbook\nRun the rollback command to undo the latest release during a severity 1 incident.\n\n"
           "[PUB-001] Office\nThe office is closed on October 31.\n\nQuestion: ")
SYSTEM = "You answer questions using ONLY the numbered sources provided in the user message."


def test_the_knowledge_brain_cites_the_best_source_or_refuses(client):
    assert "[ENG-001]" in text_of(client.post(URL, json=body(SYSTEM, SOURCES + "What command do I run to roll back the latest release during a severity 1 incident?")))
    assert text_of(client.post(URL, json=body(SYSTEM, SOURCES + "Which cafeteria vendor supplies the Berlin office lunch menu?"))) == NOT_FOUND
    assert text_of(client.post(URL, json=body(SYSTEM, "Sources:\n(no sources available)\n\nQuestion: What is the Q3 revenue forecast?"))) == NOT_FOUND


def test_a_general_knowledge_prompt_stops_refusing(client):
    system = SYSTEM + " If the sources do not fully answer, use your general knowledge."
    out = text_of(client.post(URL, json=body(system, "Sources:\n(no sources available)\n\nQuestion: What is the Q3 revenue forecast?")))
    assert out != NOT_FOUND and "[" not in out                      # uncited: the runtime citation check will hold it


def test_the_minute_quota_returns_the_real_apis_error_shape(client):
    client.sim.rpm = 3
    codes = [client.post(URL, json=body(SYSTEM, "x")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    err = client.post(URL, json=body(SYSTEM, "x")).json()["error"]
    assert err["status"] == "RESOURCE_EXHAUSTED" and any("RetryInfo" in d["@type"] for d in err["details"])
    assert "PerMinute" in err["details"][0]["violations"][0]["quotaId"]


def test_the_daily_quota_is_separate_and_stricter(client):
    client.sim.rpd = 2
    codes = [client.post(URL, json=body(SYSTEM, "x")).status_code for _ in range(3)]
    assert codes == [200, 200, 429] and "PerDay" in client.post(URL, json=body(SYSTEM, "x")).json()["error"]["details"][0]["violations"][0]["quotaId"]


def test_faults_can_be_switched_on_per_model_and_off(client):
    client.post("/_sim/faults", json={"mode": "error_500", "model": MODEL})
    assert client.post(URL, json=body(SYSTEM, "x")).status_code == 500
    assert client.post("/v1beta/models/gemini-3.5-flash:generateContent", json=body(SYSTEM, "x")).status_code == 200      # other model unaffected
    client.post("/_sim/faults", json={"mode": "none"})
    assert client.post(URL, json=body(SYSTEM, "x")).status_code == 200


def test_garbage_and_slow_faults(client):
    client.post("/_sim/faults", json={"mode": "garbage"})
    r = client.post(URL, json=body(SYSTEM, "x"))
    assert r.status_code == 200 and "<html>" in r.text
    client.post("/_sim/faults", json={"mode": "none"})
    client.post("/_sim/faults", json={"mode": "slow", "latency_ms": 300})
    t0 = time.time()
    client.post(URL, json=body(SYSTEM, "x"))
    assert time.time() - t0 >= 0.3


def test_a_timed_fault_expires(client):
    client.post("/_sim/faults", json={"mode": "error_500", "for_s": 0.2})
    assert client.post(URL, json=body(SYSTEM, "x")).status_code == 500
    time.sleep(0.3)
    assert client.post(URL, json=body(SYSTEM, "x")).status_code == 200


def test_embeddings_reflect_shared_words_and_stats_report_what_the_provider_saw(client):
    a, b, c = embed("rollback release severity incident"), embed("roll back the latest release in a severity 1 incident"), embed("base salary band")
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert dot(a, b) > dot(a, c)
    r = client.post("/v1beta/models/gemini-embedding-2:batchEmbedContents", json={"requests": [{"content": {"parts": [{"text": "hello"}]}}] * 2})
    assert len(r.json()["embeddings"]) == 2 and len(r.json()["embeddings"][0]["values"]) == 256
    client.post(URL, json=body(SYSTEM, "x"))
    stats = client.get("/_sim/stats").json()
    assert stats["responses"][f"{MODEL}:200"] == 1 and stats["requests_today"][MODEL] == 1
    client.post("/_sim/reset")
    assert client.get("/_sim/stats").json()["requests_today"] == {}


def test_the_simulator_controller_refuses_a_port_something_else_already_answers_on():
    from a2a_layer.server import ServerThread, free_port
    from deploylab.loadgen import Sim as Controller
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.applications import Starlette

    async def health(_):
        return JSONResponse({"status": "ok"})

    port = free_port()
    squatter = ServerThread(Starlette(routes=[Route("/healthz", health)]), port).start()
    try:
        with pytest.raises(RuntimeError, match="already in use"):
            Controller(port=port)
    finally:
        squatter.stop()
