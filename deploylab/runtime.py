"""What every service shares at run time: probes, in-flight tracking, correlation ids and graceful shutdown.

    /healthz   liveness   "the process is up and answering"; never checks dependencies, so a dependency
                          outage cannot make an orchestrator restart a healthy service.
    /readyz    readiness  "send me traffic"; 503 while starting, while a dependency is unavailable and while
                          draining, so a load balancer takes the instance out of rotation first.

Shutdown (SIGTERM / Ctrl+C / Ctrl+Break) is three steps: (1) flip /readyz to 503 and keep serving for
DRAIN_SECONDS, (2) stop accepting connections and let requests already running finish, for up to
GRACE_SECONDS, (3) exit. A second signal skips the wait.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import signal
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Union

import uvicorn
import uvicorn.server as uvicorn_server
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from deploylab import logs
from deploylab.config import Common

log = logging.getLogger("runtime")
PROBE_PATHS = ("/healthz", "/readyz")

Check = Callable[[], Union[bool, str, Awaitable[Union[bool, str]]]]


@dataclass
class Lifecycle:
    """Mutable state behind the probes. One per service process."""

    name: str
    version: str
    listening: bool = False
    draining: bool = False
    inflight: int = 0
    checks: dict[str, Check] = field(default_factory=dict)        # hard dependencies: failing one makes the service unready
    soft_checks: dict[str, Check] = field(default_factory=dict)   # reported, never gating (the service still does useful work)
    background: dict[str, str] = field(default_factory=dict)   # name -> "pending" | "ok" | "failed: why"
    started_at: float = field(default_factory=time.time)
    _cache: dict = field(default_factory=dict)
    _refreshing: set = field(default_factory=set)

    def run_in_background(self, name: str, fn: Callable[[], None], attempts: int = 30, delay_s: float = 2.0) -> None:
        """Run slow start-up work (loading an index, warming a cache) without blocking the port from opening.
        The service is not ready until it succeeds. It retries, because the usual reason it fails at start-up is
        that a dependency is still starting; the state shows the last error while it keeps trying."""
        self.background[name] = "pending"

        def work() -> None:
            for attempt in range(1, attempts + 1):
                try:
                    fn()
                    self.background[name] = "ok"
                    logs.event(log, "startup task finished", task=name, attempt=attempt)
                    return
                except Exception as exc:  # noqa: BLE001
                    why = f"{type(exc).__name__}: {str(exc)[:120]}"
                    self.background[name] = f"pending (attempt {attempt}/{attempts}): {why}"
                    logs.event(log, "startup task failed, will retry", level=logging.WARNING, task=name, attempt=attempt, error=why)
                    if self.draining:
                        return
                    time.sleep(delay_s)
            self.background[name] = f"failed after {attempts} attempts: {why}"

        threading.Thread(target=work, name=f"startup-{name}", daemon=True).start()

    async def _refresh(self, name: str, check: Check) -> str:
        try:
            outcome = check()
            if inspect.isawaitable(outcome):
                outcome = await asyncio.wait_for(outcome, timeout=2.0)
            value = "ok" if outcome is True else (outcome if isinstance(outcome, str) else "unavailable")
        except Exception as exc:  # noqa: BLE001
            value = f"unavailable: {type(exc).__name__}"
        self._cache[name] = (time.monotonic(), value)
        self._refreshing.discard(name)
        return value

    async def _run(self, name: str, check: Check, ttl_s: float) -> str:
        """Stale-while-revalidate: a probe is answered from the last result and never waits on a slow dependency
        (connecting to a dead port can take over two seconds, longer than most probe timeouts). Only the very
        first evaluation waits; the server runs it once at start-up so even that is already done."""
        hit = self._cache.get(name)
        if hit is None:
            return await self._refresh(name, check)
        if time.monotonic() - hit[0] >= ttl_s and name not in self._refreshing:
            self._refreshing.add(name)
            asyncio.get_running_loop().create_task(self._refresh(name, check))
        return hit[1]

    async def evaluate(self, ttl_s: float = 2.0) -> tuple[bool, dict]:
        """Hard checks and start-up tasks decide readiness. A soft check (a dependency whose loss degrades one
        capability but not the service) is reported under `soft:`; gating on it would take every instance out of
        rotation during that dependency's outage and turn a partial failure into a total one."""
        results: dict[str, str] = dict(self.background)
        names = [(n, c, False) for n, c in self.checks.items()] + [(f"soft:{n}", c, True) for n, c in self.soft_checks.items()]
        values = await asyncio.gather(*[self._run(n, c, ttl_s) for n, c, _ in names])
        soft = {}
        for (n, _, is_soft), v in zip(names, values):
            (soft if is_soft else results)[n] = v
        ok = all(v == "ok" for v in results.values())
        results.update(soft)
        return ok, results


class RuntimeMiddleware:
    """Pure ASGI middleware: correlation id, in-flight count, one structured access-log line per request."""

    def __init__(self, app, lifecycle: Lifecycle) -> None:
        self.app, self.lc = app, lifecycle

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        probe = path in PROBE_PATHS
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        cid = headers.get("x-correlation-id") or uuid.uuid4().hex[:12]
        token = logs.correlation_id.set(cid)
        status = {"code": 0}
        t0 = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                extra = [(b"x-correlation-id", cid.encode()), (b"x-service", self.lc.name.encode())]
                message = {**message, "headers": list(message.get("headers", [])) + extra}
            await send(message)

        if not probe:
            self.lc.inflight += 1
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            status["code"] = 500
            log.exception("unhandled error")
            raise
        finally:
            if not probe:
                self.lc.inflight -= 1
                logs.event(log, "request", method=scope["method"], path=path, status=status["code"],
                           ms=round((time.perf_counter() - t0) * 1000, 1))
            logs.correlation_id.reset(token)


def probe_routes(lc: Lifecycle) -> list[Route]:
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": lc.name, "version": lc.version,
                             "uptime_s": round(time.time() - lc.started_at, 1)})

    async def readyz(_: Request) -> JSONResponse:
        if lc.draining:
            return JSONResponse({"status": "draining", "service": lc.name, "inflight": lc.inflight}, status_code=503)
        ok, checks = await lc.evaluate()
        body = {"status": "ready" if ok else "unready", "service": lc.name, "version": lc.version,
                "checks": checks, "inflight": lc.inflight}
        return JSONResponse(body, status_code=200 if ok else 503)

    return [Route("/healthz", healthz), Route("/readyz", readyz)]


async def internal_error(request: Request, exc: Exception) -> JSONResponse:
    """A bug must reach the caller as JSON with the id to quote, not as a bare "Internal Server Error"."""
    cid = request.headers.get("x-correlation-id", "")
    return JSONResponse({"error": "internal", "correlation_id": cid}, status_code=500)


def make_app(lc: Lifecycle, routes: list[Route]) -> Starlette:
    app = Starlette(routes=routes + probe_routes(lc), exception_handlers={Exception: internal_error})
    app.add_middleware(RuntimeMiddleware, lifecycle=lc)       # outermost, so every request is counted and logged
    return app


def add_runtime(app, lc: Lifecycle):
    """For a third-party Starlette app (ADK's A2A app): add probes and the runtime middleware to it."""
    for r in reversed(probe_routes(lc)):
        app.router.routes.insert(0, r)
    app.add_middleware(RuntimeMiddleware, lifecycle=lc)
    return app


class DrainingServer(uvicorn.Server):
    """uvicorn with a drain step: on the first stop signal, report unready first, stop accepting later."""

    def __init__(self, config: uvicorn.Config, lc: Lifecycle, drain_s: float) -> None:
        super().__init__(config)
        self.lc, self.drain_s = lc, drain_s

    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self.lc.listening = True
            await self.lc.evaluate()                       # warm the dependency state before the first probe arrives
            logs.event(log, "listening", host=self.config.host, port=self.config.port)

    @contextlib.contextmanager
    def capture_signals(self):
        """uvicorn re-raises the stop signal after a clean shutdown, so the process dies "by signal" (exit code 3 on
        Windows, 143 on Linux). An orchestrator reads that as a failure; a service that drained and finished its
        work should exit 0."""
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        original = {sig: signal.signal(sig, self.handle_exit) for sig in uvicorn_server.HANDLED_SIGNALS}
        try:
            yield
        finally:
            for sig, handler in original.items():
                signal.signal(sig, handler)

    def handle_exit(self, sig, frame) -> None:
        if self.lc.draining:                       # a second signal means stop waiting
            super().handle_exit(sig, frame)
            return
        self.lc.draining = True
        logs.event(log, "stop signal received, draining", signal=int(sig), drain_s=self.drain_s, inflight=self.lc.inflight)
        threading.Timer(self.drain_s, lambda: uvicorn.Server.handle_exit(self, sig, frame)).start()


def serve(app, lc: Lifecycle, common: Common) -> None:
    logs.event(log, "starting", **common.summary())
    config = uvicorn.Config(app, host=common.host, port=common.port, log_config=None, access_log=False,
                            timeout_graceful_shutdown=int(common.grace_seconds), lifespan="on")
    DrainingServer(config, lc, common.drain_seconds).run()
    logs.event(log, "stopped", inflight_at_exit=lc.inflight)
