"""Monitor service: synthetic quality probes and the drift verdict, exported as metrics.

It plays the part of a user of the knowledge assistant on a schedule (see deploylab/monitor.py), so that answer quality
and access control have a signal even when no real user is asking. Everything it learns leaves as OpenTelemetry metrics;
`GET /state` shows the same thing for a person (behind the admin key).

It makes model calls (one probe per tick), so the interval is a cost setting: PROBE_INTERVAL_S seconds between probes.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import threading
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_layer.client import KnowledgeClient
from deploylab import logs
from deploylab.config import Common, Env
from deploylab.gate import normalize
from deploylab.monitor import Monitor
from deploylab.remote import RemoteIdentity
from deploylab.runtime import Lifecycle, init_telemetry, make_app, serve

log = logging.getLogger("monitor")
SERVICE = "monitor-agent"
DEFAULT_BASELINE = Path(__file__).resolve().parent.parent / "baselines" / "production_baseline.json"


class Settings:
    def __init__(self, env: Env) -> None:
        self.common = Common(env, "monitor", 8105)
        self.gateway_url = env.str("GATEWAY_URL", required=True)
        self.gateway_key = env.secret("GATEWAY_SERVICE_KEY", min_length=16)
        self.knowledge_url = env.str("KNOWLEDGE_URL", required=True)
        self.identity_url = env.str("IDENTITY_URL", required=True)
        self.interval_s = env.float("PROBE_INTERVAL_S", 60.0, minimum=1.0)
        self.baseline_path = Path(env.str("BASELINE_PATH", str(DEFAULT_BASELINE)))
        self.admin_key = env.secret("ADMIN_KEY", required=False, min_length=16)
        env.done()


def create_app(settings: Settings, monitor: Monitor = None, start_loop: bool = True):
    common = settings.common
    lc = Lifecycle(common.service, common.version)
    stop = threading.Event()
    if monitor is None:
        identity = RemoteIdentity(settings.identity_url)
        client = KnowledgeClient(settings.knowledge_url)

        def ask(case) -> str:
            token = identity.login(case.user, "knowledge-agent")
            return normalize(asyncio.run(client.ask(case.question, token, f"probe-{case.id}", subject=case.user)))

        def gateway_status() -> dict:
            r = httpx.get(f"{settings.gateway_url.rstrip('/')}/v1/status", timeout=10,
                          headers={"X-Service": SERVICE, "X-Service-Key": settings.gateway_key.reveal()})
            r.raise_for_status()
            return r.json()

        monitor = Monitor(ask, gateway_status, settings.baseline_path)

    def loop() -> None:
        while not stop.wait(settings.interval_s):
            if lc.draining:
                return
            try:
                monitor.tick()
            except Exception:  # noqa: BLE001 - the monitor keeps running; a failed tick is logged
                log.exception("monitor tick failed")

    if start_loop:
        threading.Thread(target=loop, name="monitor-loop", daemon=True).start()

    async def state(request: Request) -> JSONResponse:
        given, key = request.headers.get("x-admin-key", ""), settings.admin_key.reveal()
        if not key or not given or not hmac.compare_digest(key, given):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(monitor.snapshot())

    lc.soft_checks["knowledge"] = lambda: _ready(settings.knowledge_url)
    return make_app(lc, [Route("/state", state)]), lc, monitor


async def _ready(url: str) -> object:
    async with httpx.AsyncClient(timeout=2.5) as client:
        r = await client.get(f"{url.rstrip('/')}/readyz")
    return True if r.status_code == 200 else f"not ready ({r.status_code})"


def main() -> None:
    try:
        settings = Settings(Env())
    except Exception as exc:  # ConfigError
        logs.setup("monitor")
        log.error("%s", exc)
        raise SystemExit(2)
    logs.setup(settings.common.service, settings.common.log_level)
    init_telemetry(settings.common)
    app, lc, _ = create_app(settings)
    serve(app, lc, settings.common)


if __name__ == "__main__":
    main()
