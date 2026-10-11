"""Releases: the things that change an AI system's behaviour without a code change, versioned and rolled out safely.

A **release** is an immutable bundle of the prompts, the model pins and the tunable parameters. Changing any of
them changes what the system says, so each change gets an id, a content digest, a lint, an evaluation gate
(see deploylab/gate.py) and a rollout that can be undone in one call.

    Release        immutable manifest + digest
    Registry       a directory of manifests (releases/<id>.json)
    lint(release)  the cheap checks that catch the silly mistakes before any model is called
    ReleaseSet     the releases a running service holds: which is active, an optional canary share, an
                   optional shadow release, per-release outcome statistics, and the automatic rollback rule
    admin_routes   the HTTP surface operators use to load, activate, canary, shadow and roll back

The system under deployment keeps its prompts as module constants. `patched_prompts` swaps them for the
duration of building one component, so several releases can coexist in one process (each built component keeps
the prompt it was built with).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib
import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# release prompt name -> (module, attribute) in the system under deployment
PROMPT_TARGETS = {
    "triage": ("tickets.platform", "TRIAGE_PROMPT"),
    "resolver": ("tickets.platform", "RESOLVER_PROMPT"),
    "policy_addendum": ("tickets.platform", "POLICY_ADDENDUM"),
    "responder": ("tickets.platform", "RESPONDER_PROMPT"),
    "answerer": ("knowledge.assistant", "SYSTEM_PROMPT"),
}
REQUIRED_MODELS = ("light", "heavy")
MAX_PROMPT_CHARS = 4000
_SECRET_LIKE = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|(?i:bearer\s+[A-Za-z0-9_\-\.]{16,})|sk-[A-Za-z0-9]{20,}")
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    id: str
    prompts: dict
    models: dict
    params: dict = field(default_factory=dict)
    parent: Optional[str] = None
    notes: str = ""

    @property
    def digest(self) -> str:
        """Identifies the behaviour-defining content only (not the id, parent or notes), so two ids with the same
        digest are the same release under different names."""
        body = json.dumps({"prompts": self.prompts, "models": self.models, "params": self.params}, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]

    def to_json(self) -> dict:
        return {**asdict(self), "digest": self.digest}

    @classmethod
    def from_json(cls, data: dict) -> "Release":
        known = {"id", "prompts", "models", "params", "parent", "notes"}
        extra = set(data) - known - {"digest"}
        if extra:
            raise ReleaseError(f"unknown manifest fields: {sorted(extra)}")
        rel = cls(**{k: data[k] for k in known if k in data})
        if "digest" in data and data["digest"] != rel.digest:
            raise ReleaseError(f"{rel.id}: digest mismatch (manifest says {data['digest']}, content is {rel.digest}); the file was edited after it was written")
        return rel


class Registry:
    """Releases on disk, one JSON file each. Ids are file names, so they are restricted to a safe alphabet."""

    _ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def path(self, release_id: str) -> Path:
        if not self._ID.match(release_id):
            raise ReleaseError(f"invalid release id {release_id!r}")
        return self.dir / f"{release_id}.json"

    def save(self, release: Release) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(release.id)
        p.write_text(json.dumps(release.to_json(), indent=2, sort_keys=True), encoding="utf-8")
        return p

    def get(self, release_id: str) -> Release:
        p = self.path(release_id)
        if not p.exists():
            raise ReleaseError(f"no such release: {release_id}")
        return Release.from_json(json.loads(p.read_text(encoding="utf-8")))

    def ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.exists() else []


def lint(release: Release) -> list[str]:
    """Problems that never need a model call to find. An empty list means the manifest is well formed."""
    problems = []
    for name in PROMPT_TARGETS:
        if name not in release.prompts:
            problems.append(f"missing prompt: {name}")
    for name in release.prompts:
        if name not in PROMPT_TARGETS:
            problems.append(f"unknown prompt name {name!r} (it would be silently ignored)")       # a typo must not pass quietly
    for name, text in release.prompts.items():
        if not isinstance(text, str) or not text.strip():
            problems.append(f"prompt {name} is empty")
        elif len(text) > MAX_PROMPT_CHARS:
            problems.append(f"prompt {name} is {len(text)} characters (limit {MAX_PROMPT_CHARS})")
        elif _SECRET_LIKE.search(text):
            problems.append(f"prompt {name} contains something that looks like a credential")
    if "ask_policy" not in release.prompts.get("policy_addendum", "ask_policy"):
        problems.append("policy_addendum no longer names the ask_policy tool the resolver is given")
    for key in REQUIRED_MODELS:
        value = release.models.get(key, "")
        if not value:
            problems.append(f"missing model pin: {key}")
        elif not _MODEL_NAME.match(value):
            problems.append(f"model pin {key}={value!r} is not a plausible model id")
    return problems


_PATCH_LOCK = threading.RLock()


@contextlib.contextmanager
def patched_prompts(prompts: dict):
    """Make the system under deployment read these prompts while a component is being BUILT.

    A built `TicketPlatform` / `KnowledgeAssistant` keeps the prompt it was built with, so after the block the
    module constants go back and other releases are unaffected. The lock makes concurrent builds safe.
    """
    with _PATCH_LOCK:
        saved = []
        try:
            for name, text in prompts.items():
                module, attr = PROMPT_TARGETS[name]
                mod = importlib.import_module(module)
                saved.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, text)
            yield
        finally:
            for mod, attr, original in reversed(saved):
                setattr(mod, attr, original)


def current_prompts() -> dict:
    """The prompts the code ships with, as a release's prompt section."""
    return {name: getattr(importlib.import_module(m), a) for name, (m, a) in PROMPT_TARGETS.items()}


# ---- rollout ------------------------------------------------------------------------------------------
@dataclass
class Sample:
    outcome: str          # released | held | escalated | error
    latency_s: float
    at: float


@dataclass
class Guard:
    """When does a canary get rolled back automatically?"""

    min_requests: int = 6          # canary requests seen before the rule may fire (below this, the sample is too small)
    max_excess_bad_rate: float = 0.30   # canary bad-outcome rate minus control bad-outcome rate
    bad_outcomes: tuple = ("held", "error")


class ReleaseSet:
    """The releases one service holds, and who gets which.

    * `active` serves everyone except the canary share.
    * `canary` (id, percent) serves a stable share of requests, chosen by hashing a request key, so one ticket
      id always lands on the same side.
    * `shadow` runs on a copy of active traffic after the real answer is sent; its answers are compared, never used.
    * the guard compares canary and control outcomes since the canary started and rolls the canary back on its own.
    """

    def __init__(self, name: str, guard: Optional[Guard] = None, clock: Callable[[], float] = time.time) -> None:
        self.name, self.guard, self._clock = name, guard or Guard(), clock
        self._lock = threading.RLock()
        self.components: dict[str, Any] = {}
        self.meta: dict[str, dict] = {}
        self.active: Optional[str] = None
        self.history: list[str] = []
        self.canary: Optional[tuple[str, float]] = None
        self.shadow: Optional[str] = None
        self.events: list[dict] = []
        self.totals: dict[str, deque] = {}
        self._window: dict[str, deque] = {"canary": deque(maxlen=500), "control": deque(maxlen=500)}
        self.shadow_stats = {"n": 0, "same_action": 0, "same_category": 0, "same_severity": 0, "candidate_errors": 0}

    # -- membership ---------------------------------------------------------------------------------
    def add(self, release: Release, component: Any, make_active: bool = False) -> None:
        with self._lock:
            self.components[release.id] = component
            self.meta[release.id] = {"digest": release.digest, "parent": release.parent, "models": release.models}
            self.totals.setdefault(release.id, deque(maxlen=500))
            if make_active or self.active is None:
                self._set_active(release.id, "initial" if self.active is None else "activate")

    def _event(self, kind: str, **kw) -> dict:
        ev = {"at": round(self._clock(), 3), "type": kind, **kw}
        self.events.append(ev)
        return ev

    def _set_active(self, release_id: str, kind: str) -> None:
        if self.active is not None and self.active != release_id:
            self.history.append(self.active)
        self._event(kind, release=release_id, previous=self.active)
        self.active = release_id

    def _require(self, release_id: str) -> None:
        if release_id not in self.components:
            raise ReleaseError(f"release {release_id} is not loaded")

    # -- operator actions ---------------------------------------------------------------------------
    def activate(self, release_id: str) -> None:
        with self._lock:
            self._require(release_id)
            self.canary = None
            self._set_active(release_id, "activate")

    def set_canary(self, release_id: str, percent: float) -> None:
        with self._lock:
            self._require(release_id)
            if not 0 < percent <= 50:
                raise ReleaseError("canary percent must be above 0 and at most 50")
            if release_id == self.active:
                raise ReleaseError("the canary must be a different release from the active one")
            self.canary = (release_id, float(percent))
            self._window = {"canary": deque(maxlen=500), "control": deque(maxlen=500)}
            self._event("canary", release=release_id, percent=percent)

    def set_shadow(self, release_id: Optional[str]) -> None:
        with self._lock:
            if release_id is not None:
                self._require(release_id)
            self.shadow = release_id
            self.shadow_stats = {k: 0 for k in self.shadow_stats}
            self._event("shadow", release=release_id)

    def rollback(self, reason: str = "operator") -> str:
        """Back to the previously active release; clears any canary and shadow. Returns the release now active."""
        with self._lock:
            if self.canary is not None:                           # a canary in progress: ending it IS the rollback
                gone, self.canary = self.canary[0], None
                self._event("rollback", release=gone, reason=reason, restored=self.active)
                return self.active  # type: ignore[return-value]
            if not self.history:
                raise ReleaseError("nothing to roll back to")
            bad, self.active = self.active, self.history.pop()
            self.shadow = None
            self._event("rollback", release=bad, reason=reason, restored=self.active)
            return self.active

    # -- request path -------------------------------------------------------------------------------
    def choose(self, key: str) -> tuple[str, str]:
        """(release id, role) for a request. Stable: the same key always goes to the same side of a canary."""
        with self._lock:
            if self.active is None:
                raise ReleaseError("no active release")
            if self.canary is not None:
                bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % 10_000 / 100.0
                if bucket < self.canary[1]:
                    return self.canary[0], "canary"
                return self.active, "control"
            return self.active, "active"

    def component(self, release_id: str) -> Any:
        with self._lock:
            return self.components[release_id]

    def record(self, release_id: str, role: str, outcome: str, latency_s: float) -> Optional[dict]:
        """Note one finished request; may roll the canary back. Returns the rollback event if it fired."""
        sample = Sample(outcome, latency_s, self._clock())
        with self._lock:
            self.totals.setdefault(release_id, deque(maxlen=500)).append(sample)
            if role in self._window and self.canary is not None:
                self._window[role].append(sample)
                return self._evaluate_guard()
        return None

    def _bad_rate(self, samples) -> float:
        return sum(1 for s in samples if s.outcome in self.guard.bad_outcomes) / len(samples) if samples else 0.0

    def _evaluate_guard(self) -> Optional[dict]:
        canary, control = self._window["canary"], self._window["control"]
        if self.canary is None or len(canary) < self.guard.min_requests or not control:
            return None
        excess = self._bad_rate(canary) - self._bad_rate(control)
        if excess >= self.guard.max_excess_bad_rate:
            gone, self.canary = self.canary[0], None
            return self._event("auto_rollback", release=gone, restored=self.active,
                               reason=f"canary bad-outcome rate {self._bad_rate(canary):.0%} vs control {self._bad_rate(control):.0%} "
                                      f"after {len(canary)} canary requests (limit +{self.guard.max_excess_bad_rate:.0%})")
        return None

    def record_shadow(self, same_action: bool, same_category: bool, same_severity: bool, error: bool) -> None:
        with self._lock:
            s = self.shadow_stats
            s["n"] += 1
            s["same_action"] += int(same_action)
            s["same_category"] += int(same_category)
            s["same_severity"] += int(same_severity)
            s["candidate_errors"] += int(error)

    # -- reporting ----------------------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            def stats(samples):
                n = len(samples)
                lat = sorted(s.latency_s for s in samples)
                return {"n": n, "bad_rate": round(self._bad_rate(samples), 3),
                        "p50_s": round(lat[n // 2], 2) if n else None}
            return {"service": self.name, "active": self.active, "previous": self.history[-1] if self.history else None,
                    "canary": ({"release": self.canary[0], "percent": self.canary[1]} if self.canary else None),
                    "shadow": self.shadow, "shadow_stats": dict(self.shadow_stats),
                    "loaded": {rid: {**self.meta[rid], **stats(self.totals[rid])} for rid in self.components},
                    "window": {k: stats(v) for k, v in self._window.items()}, "events": list(self.events[-20:])}


# ---- admin surface ------------------------------------------------------------------------------------
def admin_routes(rs: ReleaseSet, registry: Registry, build: Callable[[Release], Any], admin_key: str, prefix: str = "/admin/releases") -> list[Route]:
    """Operator endpoints, all behind X-Admin-Key. `build(release)` makes this service's component for a release."""

    def authorized(request: Request) -> bool:
        given = request.headers.get("x-admin-key", "")
        return bool(admin_key) and bool(given) and hmac.compare_digest(admin_key, given)

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def guarded(fn):
        async def handler(request: Request) -> JSONResponse:
            if not authorized(request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return JSONResponse(await fn(request))
            except ReleaseError as exc:
                return JSONResponse({"error": "release", "detail": str(exc)}, status_code=409)
        return handler

    async def status(_: Request) -> dict:
        return rs.status()

    async def load(request: Request) -> dict:
        rid = (await body(request)).get("id", "")
        release = registry.get(rid)
        problems = lint(release)
        if problems:
            raise ReleaseError(f"{rid} failed lint: {'; '.join(problems)}")
        if rid not in rs.components:
            import asyncio
            rs.add(release, await asyncio.to_thread(build, release))
        return {"loaded": rid, "digest": release.digest}

    async def activate(request: Request) -> dict:
        rs.activate((await body(request)).get("id", ""))
        return rs.status()

    async def canary(request: Request) -> dict:
        data = await body(request)
        rs.set_canary(data.get("id", ""), float(data.get("percent", 0)))
        return rs.status()

    async def shadow(request: Request) -> dict:
        rs.set_shadow((await body(request)).get("id"))
        return rs.status()

    async def rollback(request: Request) -> dict:
        rs.rollback((await body(request)).get("reason", "operator"))
        return rs.status()

    return [Route(prefix, guarded(status)), Route(f"{prefix}/load", guarded(load), methods=["POST"]),
            Route(f"{prefix}/activate", guarded(activate), methods=["POST"]), Route(f"{prefix}/canary", guarded(canary), methods=["POST"]),
            Route(f"{prefix}/shadow", guarded(shadow), methods=["POST"]), Route(f"{prefix}/rollback", guarded(rollback), methods=["POST"])]
