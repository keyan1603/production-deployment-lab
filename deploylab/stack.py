"""Run the four services as separate OS processes on one machine, the way an orchestrator would.

What this does on purpose, because production does it:

* every service is its own process with its own environment; the model provider's API key is given to the
  gateway process ONLY (the others receive a minimal environment, not a copy of this shell's);
* secrets are written to files and passed as `NAME_FILE`, never on the command line or in the environment;
* services are started all at once and find each other through readiness, not through a start-up order;
* stopping sends the platform's polite stop signal (Ctrl+Break on Windows, SIGTERM elsewhere), so the
  drain-then-finish shutdown is exercised, and the exit code and time are recorded.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORTS = {"identity": 8101, "gateway": 8102, "knowledge": 8103, "tickets": 8104, "monitor": 8105}
OPTIONAL = {"monitor"}          # makes model calls on a schedule, so it only runs when asked for
MODULES = {"identity": "services.identity_service", "gateway": "services.gateway_service",
           "knowledge": "services.knowledge_service", "tickets": "services.ticket_service",
           "monitor": "services.monitor_service"}
_PASS_THROUGH = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                 "APPDATA", "LOCALAPPDATA", "HOME", "LANG", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMDATA")


@dataclass
class Proc:
    name: str
    popen: subprocess.Popen
    log_path: Path
    started: float = field(default_factory=time.time)


def base_env() -> dict:
    env = {k: os.environ[k] for k in _PASS_THROUGH if k in os.environ}
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT)})
    return env


class Stack:
    def __init__(self, run_dir: Optional[Path] = None, gemini_key: str = "", pin_light: bool = True,
                 rpm: int = 15, overrides: Optional[dict] = None, version: str = "dev", port_base: int = 8100,
                 release: str = "r1-baseline", models: Optional[dict] = None, otel_endpoint: str = "", probe_interval_s: float = 60.0, provider_url: str = "") -> None:
        self.run = Path(run_dir or ROOT / "run")
        self.secrets_dir, self.logs_dir = self.run / "secrets", self.run / "logs"
        self.gemini_key, self.pin_light, self.rpm, self.version = gemini_key, pin_light, rpm, version
        self.overrides = overrides or {}
        self.procs: dict[str, Proc] = {}
        self.ports = {n: port_base + (p - 8100) for n, p in PORTS.items()}
        self.urls = {n: f"http://127.0.0.1:{p}" for n, p in self.ports.items()}
        self.release, self.models = release, models or {}
        self.otel_endpoint, self.probe_interval_s, self.provider_url = otel_endpoint, probe_interval_s, provider_url
        self.admin_key = (self.secrets_dir / "admin_key").read_text() if (self.secrets_dir / "admin_key").exists() else secrets.token_urlsafe(24)

    # -- secrets ----------------------------------------------------------------------------------
    def _write_secrets(self) -> None:
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        (self.secrets_dir / "token_secret").write_text(secrets.token_urlsafe(48))
        ticket_key, know_key, mon_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        (self.secrets_dir / "gateway_key_monitor").write_text(mon_key)
        (self.secrets_dir / "gateway_key_ticket").write_text(ticket_key)
        (self.secrets_dir / "gateway_key_knowledge").write_text(know_key)
        (self.secrets_dir / "gateway_service_keys").write_text(f"ticket-agent={ticket_key},knowledge-agent={know_key},monitor-agent={mon_key}")
        (self.secrets_dir / "gemini_key").write_text(self.gemini_key)
        (self.secrets_dir / "admin_key").write_text(self.admin_key)

    def _env_for(self, name: str) -> dict:
        s = self.secrets_dir
        env = base_env()
        if self.otel_endpoint:
            env["OTEL_EXPORTER_OTLP_ENDPOINT"] = self.otel_endpoint
        env.update({"ENVIRONMENT": "local", "APP_VERSION": self.version, "HOST": "127.0.0.1", "PORT": str(self.ports[name]),
                    "DRAIN_SECONDS": "2", "GRACE_SECONDS": "60"})
        if name == "identity":
            env["TOKEN_SECRET_FILE"] = str(s / "token_secret")
        elif name == "gateway":
            env.update({"GEMINI_API_KEY_FILE": str(s / "gemini_key"), "GATEWAY_SERVICE_KEYS_FILE": str(s / "gateway_service_keys"),
                        "REQUESTS_PER_MINUTE": str(self.rpm), "PIN_LIGHT": str(self.pin_light).lower(),
                        "AUDIT_PATH": str(self.run / "audit" / "gateway.jsonl")})
            if self.provider_url:                # the Google SDK reads this: every model call goes to the simulator instead of the real API
                env["GOOGLE_GEMINI_BASE_URL"] = self.provider_url
            if self.models.get("light"):
                env["LIGHT_MODEL"] = self.models["light"]
            if self.models.get("heavy"):
                env["HEAVY_MODEL"] = self.models["heavy"]
        elif name == "knowledge":
            env.update({"TOKEN_SECRET_FILE": str(s / "token_secret"), "GATEWAY_URL": self.urls["gateway"],
                        "GATEWAY_SERVICE_KEY_FILE": str(s / "gateway_key_knowledge"),
                        "AUDIT_PATH": str(self.run / "audit" / "knowledge_agent.jsonl")})
        elif name == "tickets":
            env.update({"TOKEN_SECRET_FILE": str(s / "token_secret"), "GATEWAY_URL": self.urls["gateway"],
                        "GATEWAY_SERVICE_KEY_FILE": str(s / "gateway_key_ticket"), "KNOWLEDGE_URL": self.urls["knowledge"],
                        "IDENTITY_URL": self.urls["identity"]})
        elif name == "monitor":
            env.update({"GATEWAY_URL": self.urls["gateway"], "GATEWAY_SERVICE_KEY_FILE": str(s / "gateway_key_monitor"),
                        "KNOWLEDGE_URL": self.urls["knowledge"], "IDENTITY_URL": self.urls["identity"],
                        "PROBE_INTERVAL_S": str(self.probe_interval_s), "ADMIN_KEY_FILE": str(s / "admin_key")})
        if name in ("knowledge", "tickets"):
            env.update({"ACTIVE_RELEASE": self.release, "RELEASES_DIR": str(ROOT / "releases"),
                        "ADMIN_KEY_FILE": str(s / "admin_key")})
        env.update(self.overrides.get(name, {}))
        return env

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self, names: Optional[list] = None) -> None:
        if not self.secrets_dir.exists():
            self._write_secrets()
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.run / "audit").mkdir(parents=True, exist_ok=True)
        for name in names or [n for n in MODULES if n not in OPTIONAL]:
            self.spawn(name)

    def spawn(self, name: str) -> Proc:
        log_path = self.logs_dir / f"{name}.log"
        log_file = open(log_path, "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        popen = subprocess.Popen([sys.executable, "-m", MODULES[name]], cwd=ROOT, env=self._env_for(name),
                                 stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags)
        self.procs[name] = Proc(name, popen, log_path)
        return self.procs[name]

    def spawn_extra(self, name: str, key: str, port: int, env: Optional[dict] = None) -> Proc:
        """Another instance of a service on its own port (for scale-out experiments). It is addressed as `name-key`."""
        full = f"{name}-{key}"
        self.ports[full], self.urls[full] = port, f"http://127.0.0.1:{port}"
        child_env = {**self._env_for(name), "PORT": str(port), **(env or {})}
        log_path = self.logs_dir / f"{full}.log"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        popen = subprocess.Popen([sys.executable, "-m", MODULES[name]], cwd=ROOT, env=child_env, stdout=open(log_path, "ab"),
                                 stderr=subprocess.STDOUT, creationflags=flags)
        self.procs[full] = Proc(full, popen, log_path)
        return self.procs[full]

    def probe(self, name: str, path: str = "/readyz", timeout: float = 4.0) -> tuple[int, dict]:
        try:
            r = httpx.get(f"{self.urls[name]}{path}", timeout=timeout)
            return r.status_code, (r.json() if r.content else {})
        except httpx.HTTPError:
            return 0, {}

    def wait_ready(self, names: Optional[list] = None, timeout: float = 180.0) -> dict:
        """Seconds until each service reported ready; raises with the log tail if one dies or never gets ready."""
        pending, ready, t0 = set(names or self.procs), {}, time.time()
        while pending and time.time() - t0 < timeout:
            for name in sorted(pending):
                if self.procs[name].popen.poll() is not None:
                    raise RuntimeError(f"{name} exited with code {self.procs[name].popen.returncode}\n{self.tail(name)}")
                code, _ = self.probe(name)
                if code == 200:
                    ready[name] = round(time.time() - t0, 1)
                    pending.discard(name)
            time.sleep(0.4)
        if pending:
            raise RuntimeError(f"not ready after {timeout}s: {sorted(pending)}\n" + "\n".join(self.tail(n) for n in pending))
        return ready

    def tail(self, name: str, lines: int = 12) -> str:
        try:
            return "\n".join(self.procs[name].log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
        except OSError:
            return ""

    def stop_one(self, name: str, wait: float = 90.0) -> dict:
        proc = self.procs[name].popen
        t0 = time.time()
        if proc.poll() is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                return {"exit": "killed after timeout", "seconds": round(time.time() - t0, 1)}
        return {"exit": proc.returncode, "seconds": round(time.time() - t0, 1)}

    def stop(self) -> dict:
        """Stop in dependency order: callers first, so nothing is left calling a service that is already gone."""
        out = {}
        for name in [n for n in self.procs if "-" in n] + ["monitor", "tickets", "knowledge", "gateway", "identity"]:
            if name in self.procs:
                out[name] = self.stop_one(name)
        return out

    def environment_of(self, name: str) -> dict:
        """What the process was given (names only for secrets), for the least-privilege check."""
        return {k: ("<file>" if k.endswith("_FILE") else v) for k, v in self._env_for(name).items()}

    def __enter__(self) -> "Stack":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


def load_gemini_key() -> str:
    from dotenv import dotenv_values
    for candidate in (ROOT / ".env", ROOT.parent / "ai-system-design-lab" / ".env"):
        if candidate.exists():
            key = dotenv_values(candidate).get("GEMINI_API_KEY", "")
            if key:
                return key
    return os.environ.get("GEMINI_API_KEY", "")
