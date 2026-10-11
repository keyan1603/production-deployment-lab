"""Structured JSON logs with the correlation id on every line, and tokens scrubbed from anything written."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time

from opentelemetry import trace

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("deploylab_correlation_id", default="")

# A bearer token or service key that ends up in an exception message or a URL must not reach the log.
_SCRUB = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{12,}"), r"\1***"),
    (re.compile(r"(?i)(x-service-key[\"':=\s]+)[A-Za-z0-9_\-\.=]{8,}"), r"\1***"),
    (re.compile(r"(AIza)[0-9A-Za-z_\-]{20,}"), r"\1***"),
]


def scrub(text: str) -> str:
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return text


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        body = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        cid = correlation_id.get()
        if cid:
            body["correlation_id"] = cid
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:                                   # lets a log line lead to its trace, and a trace to its log lines
            body["trace_id"], body["span_id"] = f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"
        extra = getattr(record, "fields", None)
        if extra:
            body.update({k: (scrub(v) if isinstance(v, str) else v) for k, v in extra.items()})
        if record.exc_info:
            body["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(body, default=str)


def setup(service: str, level: str = "INFO") -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "asyncio", "a2a"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(service)


def event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
