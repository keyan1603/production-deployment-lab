"""A Gemini API simulator for load tests and incident drills. It is NOT a model.

What it is for: exercising the parts of the system that depend on how a provider BEHAVES (quotas, latency, errors,
overload), with the real gateway, the real Google SDK and the real retry logic pointed at it through
`GOOGLE_GEMINI_BASE_URL`. A load test against the real API would spend a free-tier quota of 500 requests a day in a minute;
a drill against the real API cannot make it fail on demand.

What it is not for: judging answer quality. Its answers come from a small script (keyword rules for triage, a tool-call
flow for the resolver, source echoing for the knowledge assistant). Every result measured against it is labelled as simulated.

Behaviours, all configurable at run time:
    quota    per-model requests per minute and per day; beyond them a 429 RESOURCE_EXHAUSTED in the shape the real API uses
    latency  log-normal around a mean per model (the light model is faster than the heavy one)
    faults   429 storm, 500 errors, slow responses, garbage (non-JSON) bodies, black hole (accepts and never answers)
    stats    requests by model and status, requests in the last minute, so an experiment can read what the provider saw

    POST /v1beta/models/{model}:generateContent     POST .../:batchEmbedContents   POST .../:embedContent
    POST /_sim/faults  {"mode": "error_429|error_500|slow|garbage|blackhole|none", "model": "*", "probability": 1.0, "latency_ms": 5000}
    POST /_sim/config  {"rpm": 15, "rpd": 500, "latency_ms": {"gemini-3.5-flash-lite": 900}}
    GET  /_sim/stats   POST /_sim/reset
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import random
import re
import time
from collections import defaultdict, deque

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

NOT_FOUND = "I can't find that in the documents you have access to."
STOP = {"the", "a", "an", "is", "are", "of", "to", "in", "on", "for", "and", "or", "what", "how", "do", "does", "i", "my", "we", "can", "which", "that",
        "this", "it", "be", "by", "with", "at", "as", "if", "me", "you", "your", "our", "from", "about", "much", "many", "run", "when"}
DEFAULT_LATENCY_MS = {"gemini-3.5-flash-lite": 900.0, "gemini-3.5-flash": 2200.0}
EMBED_DIM = 256


def words(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


def embed(text: str) -> list:
    """Hashed bag of words, unit length: cosine similarity then tracks shared vocabulary."""
    vec = [0.0] * EMBED_DIM
    for w in words(text):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


# ---- the scripted brain ------------------------------------------------------------------------
def part_texts(contents: list) -> str:
    return "\n".join(p.get("text", "") for c in contents for p in c.get("parts", []) if "text" in p)


def tool_results(contents: list) -> list:
    return [p["functionResponse"] for c in contents for p in c.get("parts", []) if "functionResponse" in p]


def triage(user: str, system: str = "") -> dict:
    low = user.lower()
    category = "billing" if re.search(r"charge|refund|invoice|payment|paid", low) else "account" if re.search(r"log in|login|password|locked|account|plan", low) else "technical"
    # Follow the category names the prompt asks for (billing, technical, account by position), as a model told to rename them would.
    named = re.search(r'"category" \(([^)]*)\)', system)
    if named:
        options = [o.strip() for o in re.split(r",| or ", named.group(1)) if o.strip()]
        if len(options) == 3:
            category = options[{"billing": 0, "technical": 1, "account": 2}[category]]
    severity = "high" if re.search(r"twice|locked|crash|today|urgent", low) else "low" if re.search(r"confirm|question|plan and region", low) else "medium"
    cust, order = re.search(r"\bC-\d{4}\b", user), re.search(r"\bO-\d{4}\b", user)
    return {"category": category, "severity": severity, "customer_id": cust.group(0) if cust else None, "order_id": order.group(0) if order else None,
            "summary": re.sub(r"\s+", " ", user.strip())[:140]}


def resolver_step(system: str, contents: list, tools: list) -> dict:
    """First turn: ask for the facts. After the tool results: write the resolution note."""
    results = tool_results(contents)
    if results:
        facts = "; ".join(str(r.get("response", {}).get("result", r.get("response")))[:160] for r in results)
        return {"text": f"Resolution note: restates the ticket. Facts looked up: {facts}"}
    text = part_texts(contents)
    calls = []
    if "get_customer" in tools and (m := re.search(r"\bC-\d{4}\b", text)):
        calls.append({"name": "get_customer", "args": {"customer_id": m.group(0)}})
    if "get_order" in tools and (m := re.search(r"\bO-\d{4}\b", text)):
        calls.append({"name": "get_order", "args": {"order_id": m.group(0)}})
    if "search_kb" in tools:
        topic = "duplicate_charge" if re.search(r"twice|duplicate", text.lower()) else "suspended_account" if "locked" in text.lower() else "password_reset"
        calls.append({"name": "search_kb", "args": {"topic": topic}})
    if "ask_policy" in tools and re.search(r"approval|threshold|policy|refund", text.lower()):
        calls.append({"name": "ask_policy", "args": {"question": "What is the refund approval threshold?"}})
    return {"calls": calls} if calls else {"text": "Resolution note: no lookup was possible."}


def knowledge_answer(user: str, system: str = "") -> str:
    sources = re.findall(r"^\[([A-Z]+-\d{3})\] (.*?)\n(.*?)(?=\n\n\[|\n\nQuestion:|\Z)", user, flags=re.S | re.M)
    question = user.rsplit("Question:", 1)[-1]
    qw = set(words(question))
    best, score = None, 0
    for sid, title, body in sources:
        overlap = len(qw & set(words(title + " " + body)))
        if overlap > score:
            best, score = (sid, body), overlap
    if best is None or score < 2:
        # A prompt that tells the model to fall back on general knowledge gets an uncited answer instead of a refusal.
        return "From general knowledge, this is usually handled by the team that owns it." if "general knowledge" in system.lower() else NOT_FOUND
    sid, body = best
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    return f"{' '.join(sentences[:3])} [{sid}]"


def respond(system: str, contents: list, tools: list) -> dict:
    user = part_texts(contents)
    low = system.lower()
    if "you triage" in low:
        return {"text": json.dumps(triage(user, system))}
    if "support resolver" in low:
        return resolver_step(system, contents, tools)
    if "customer-facing reply" in low:
        note = user.split("Resolution note:", 1)[-1].strip()
        return {"text": f"Thanks for contacting us. {note[:240]} Please let us know if you need anything else."}
    if "numbered sources" in low:
        return {"text": knowledge_answer(user, system)}
    return {"text": "ok"}


# ---- the simulated provider --------------------------------------------------------------------
class Sim:
    def __init__(self, rpm: int = 15, rpd: int = 500, seed: int = 7) -> None:
        self.rpm, self.rpd = rpm, rpd
        self.rpm_by_model: dict = {}                    # e.g. {"gemini-3.5-flash": 5}
        self.rpd_by_model: dict = {}                    # e.g. {"gemini-3.5-flash": 20}: the heavy model has a much smaller free allowance
        self.latency_ms = dict(DEFAULT_LATENCY_MS)
        self.rng = random.Random(seed)
        self.reset()

    def reset(self) -> None:
        self.window: dict = defaultdict(deque)          # model -> request times in the last minute
        self.day: dict = defaultdict(int)
        self.counts: dict = defaultdict(int)            # (model, status) -> n
        self.faults: list = []
        self.inflight = 0
        self.max_inflight = 0

    def quota_error(self, model: str, which: str, limit: int, retry_s: int) -> JSONResponse:
        return JSONResponse({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                       "message": f"You exceeded your current quota, please check your plan and billing details.\n* Quota exceeded for metric: "
                                                  f"generate_content_free_tier_requests, limit: {limit}, model: {model}\nPlease retry in {retry_s}s.",
                                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                                    "violations": [{"quotaId": which, "quotaValue": str(limit), "quotaDimensions": {"model": model}}]},
                                                   {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{retry_s}s"}]}},
                            status_code=429)

    def check_quota(self, model: str):
        now = time.time()
        w = self.window[model]
        while w and now - w[0] > 60:
            w.popleft()
        limit = self.rpd_by_model.get(model, self.rpd)
        if self.day[model] >= limit:
            return self.quota_error(model, "GenerateRequestsPerDayPerProjectPerModel-FreeTier", limit, 3600)
        rpm = self.rpm_by_model.get(model, self.rpm)
        if len(w) >= rpm:
            return self.quota_error(model, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", rpm, max(1, int(60 - (now - w[0]))))
        w.append(now)
        self.day[model] += 1
        return None

    def active_fault(self, model: str):
        now = time.time()
        self.faults = [f for f in self.faults if f.get("until", now + 1) > now]
        for f in self.faults:
            if f["model"] in ("*", model) and self.rng.random() < f.get("probability", 1.0):
                return f
        return None

    def delay_s(self, model: str) -> float:
        mean = self.latency_ms.get(model, 1000.0)
        return max(0.01, self.rng.lognormvariate(math.log(mean), 0.35) / 1000.0)

    def stats(self) -> dict:
        now = time.time()
        return {"requests_last_minute": {m: sum(1 for t in w if now - t <= 60) for m, w in self.window.items()},
                "requests_today": dict(self.day), "responses": {f"{m}:{s}": n for (m, s), n in self.counts.items()},
                "max_inflight": self.max_inflight, "faults": self.faults, "config": {"rpm": self.rpm, "rpd": self.rpd, "latency_ms": self.latency_ms}}


def create_app(sim: Sim = None) -> Starlette:
    sim = sim or Sim()

    async def model_call(request: Request) -> Response:
        model, action = request.path_params["model"], request.path_params["action"]
        body = await request.json()
        if action in ("batchEmbedContents", "embedContent"):
            texts = [p.get("text", "") for r in body.get("requests", [body]) for p in (r.get("content") or {}).get("parts", [])]
            sim.counts[(model, 200)] += 1
            if action == "embedContent":
                return JSONResponse({"embedding": {"values": embed(texts[0] if texts else "")}})
            return JSONResponse({"embeddings": [{"values": embed(t)} for t in texts]})
        if action != "generateContent":
            return JSONResponse({"error": {"code": 404, "message": f"unsupported action {action}"}}, status_code=404)

        limited = sim.check_quota(model)
        if limited is not None:
            sim.counts[(model, 429)] += 1
            return limited
        fault = sim.active_fault(model)
        sim.inflight += 1
        sim.max_inflight = max(sim.max_inflight, sim.inflight)
        try:
            if fault and fault["mode"] == "blackhole":
                await asyncio.sleep(fault.get("latency_ms", 600_000) / 1000.0)
            if fault and fault["mode"] == "error_429":
                sim.counts[(model, 429)] += 1
                return sim.quota_error(model, "GenerateRequestsPerMinutePerProjectPerModel-FreeTier", sim.rpm, 30)
            if fault and fault["mode"] == "error_500":
                sim.counts[(model, 500)] += 1
                return JSONResponse({"error": {"code": 500, "status": "INTERNAL", "message": "simulated internal error"}}, status_code=500)
            await asyncio.sleep(sim.delay_s(model) + (fault.get("latency_ms", 0) / 1000.0 if fault and fault["mode"] == "slow" else 0.0))
            if fault and fault["mode"] == "garbage":
                sim.counts[(model, 200)] += 1
                return PlainTextResponse("<html>upstream proxy error</html>", status_code=200)
            system = " ".join(p.get("text", "") for p in (body.get("systemInstruction") or body.get("system_instruction") or {}).get("parts", []))
            tools = [d["name"] for t in body.get("tools", []) for d in t.get("functionDeclarations", [])]
            out = respond(system, body.get("contents", []), tools)
            parts = ([{"functionCall": c, "thoughtSignature": base64.b64encode(b"sim-signature").decode()} for c in out["calls"]]
                     if "calls" in out else [{"text": out["text"]}])
            prompt_chars = len(json.dumps(body))
            reply_chars = len(json.dumps(parts))
            sim.counts[(model, 200)] += 1
            return JSONResponse({"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
                                 "usageMetadata": {"promptTokenCount": prompt_chars // 4, "candidatesTokenCount": reply_chars // 4,
                                                   "totalTokenCount": (prompt_chars + reply_chars) // 4},
                                 "modelVersion": model})
        finally:
            sim.inflight -= 1

    async def set_faults(request: Request) -> JSONResponse:
        data = await request.json()
        if data.get("mode", "none") == "none":
            sim.faults = []
        else:
            fault = {"mode": data["mode"], "model": data.get("model", "*"), "probability": float(data.get("probability", 1.0)),
                     "latency_ms": float(data.get("latency_ms", 0))}
            if data.get("for_s"):
                fault["until"] = time.time() + float(data["for_s"])
            sim.faults.append(fault)
        return JSONResponse({"faults": sim.faults})

    async def set_config(request: Request) -> JSONResponse:
        data = await request.json()
        sim.rpm, sim.rpd = int(data.get("rpm", sim.rpm)), int(data.get("rpd", sim.rpd))
        sim.rpd_by_model.update({k: int(v) for k, v in data.get("rpd_by_model", {}).items()})
        sim.rpm_by_model.update({k: int(v) for k, v in data.get("rpm_by_model", {}).items()})
        sim.latency_ms.update({k: float(v) for k, v in data.get("latency_ms", {}).items()})
        return JSONResponse(sim.stats()["config"])

    async def stats(_: Request) -> JSONResponse:
        return JSONResponse(sim.stats())

    async def reset(_: Request) -> JSONResponse:
        sim.reset()
        return JSONResponse({"ok": True})

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(routes=[
        Route("/{version}/models/{model}:{action}", model_call, methods=["POST"]),
        Route("/_sim/faults", set_faults, methods=["POST"]), Route("/_sim/config", set_config, methods=["POST"]),
        Route("/_sim/stats", stats), Route("/_sim/reset", reset, methods=["POST"]), Route("/healthz", health)])
    app.state.sim = sim
    return app


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8900)
    ap.add_argument("--rpm", type=int, default=15)
    ap.add_argument("--rpd", type=int, default=500)
    a = ap.parse_args()
    uvicorn.run(create_app(Sim(a.rpm, a.rpd)), host=a.host, port=a.port, log_level="warning")
