"""Twelve-factor configuration: every setting comes from the environment, secrets can come from files.

Two rules this module enforces:

* Fail fast and completely. A service that is missing configuration refuses to start and lists EVERY problem
  at once, instead of dying on the first missing variable and again on the next one.
* Secrets never appear in logs. A `Secret` prints as `***`; the value is only reachable with `.reveal()`.
  A secret can be supplied as `NAME` (an environment variable) or `NAME_FILE` (a path to a file, the way
  container orchestrators mount secrets), and the file form wins.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional


class ConfigError(Exception):
    """Raised once, at startup, listing every configuration problem found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("invalid configuration:\n  - " + "\n  - ".join(problems))
        self.problems = problems


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__


class Env:
    """Reads settings and collects problems; call `.done()` after reading everything."""

    def __init__(self, environ: Optional[dict] = None) -> None:
        self._env = os.environ if environ is None else environ
        self.problems: list[str] = []

    def str(self, name: str, default: Optional[str] = None, required: bool = False) -> str:
        value = self._env.get(name)
        if value is None or value == "":
            if required and default is None:
                self.problems.append(f"{name} is required")
                return ""
            return default or ""
        return value

    def cast(self, name: str, fn: Callable, default, check: Optional[Callable] = None, why: str = "") -> object:
        raw = self._env.get(name)
        if raw is None or raw == "":
            return default
        try:
            value = fn(raw)
        except (TypeError, ValueError):
            self.problems.append(f"{name}={raw!r} is not a valid {fn.__name__}")
            return default
        if check is not None and not check(value):
            self.problems.append(f"{name}={raw!r} is out of range{(' (' + why + ')') if why else ''}")
            return default
        return value

    def int(self, name: str, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
        return self.cast(name, int, default, lambda v: v >= minimum and (maximum is None or v <= maximum),
                         f"{minimum}..{maximum if maximum is not None else 'any'}")  # type: ignore[return-value]

    def float(self, name: str, default: float, minimum: float = 0.0) -> float:
        return self.cast(name, float, default, lambda v: v >= minimum, f">= {minimum}")  # type: ignore[return-value]

    def secret(self, name: str, required: bool = True, min_length: int = 0) -> Secret:
        path = self._env.get(f"{name}_FILE")
        value = None
        if path:
            try:
                value = Path(path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                self.problems.append(f"{name}_FILE={path!r} cannot be read ({exc.strerror or exc})")
                return Secret("")
        elif self._env.get(name):
            value = self._env[name].strip()
        if not value:
            if required:
                self.problems.append(f"{name} (or {name}_FILE) is required")
            return Secret("")
        if min_length and len(value) < min_length:
            self.problems.append(f"{name} must be at least {min_length} characters")
        return Secret(value)

    def done(self) -> None:
        if self.problems:
            raise ConfigError(self.problems)


class Common:
    """Settings every service shares."""

    def __init__(self, env: Env, service: str, default_port: int) -> None:
        self.service = service
        self.environment = env.str("ENVIRONMENT", "local")
        self.version = env.str("APP_VERSION", "dev")
        self.host = env.str("HOST", "127.0.0.1")
        self.port = env.int("PORT", default_port, minimum=1, maximum=65535)
        self.log_level = env.str("LOG_LEVEL", "INFO").upper()
        # Drain: after the stop signal, keep serving for this long with /readyz failing, so a load balancer
        # notices and stops sending new work. Grace: then wait up to this long for requests already running.
        self.drain_seconds = env.float("DRAIN_SECONDS", 3.0)
        self.grace_seconds = env.float("GRACE_SECONDS", 60.0)
        # Transport security between services: both files, or neither. With them the service speaks HTTPS; clients
        # trust the CA through SSL_CERT_FILE. (ADK's A2A client refuses a plain-http agent card off loopback.)
        cert, key = env.str("TLS_CERT_FILE", ""), env.str("TLS_KEY_FILE", "")
        if bool(cert) != bool(key):
            env.problems.append("TLS_CERT_FILE and TLS_KEY_FILE must be set together")
        self.tls_cert_file, self.tls_key_file = cert or None, key or None
        scheme = "https" if cert and key else "http"
        self.public_url = env.str("PUBLIC_URL", "") or f"{scheme}://{self.host}:{self.port}"

    def summary(self) -> dict:
        return {"service": self.service, "environment": self.environment, "version": self.version,
                "host": self.host, "port": self.port, "drain_s": self.drain_seconds, "grace_s": self.grace_seconds}
