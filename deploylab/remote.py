"""Clients for the lab's network services, shaped like the in-process objects the #17 code already uses.

The ticket and knowledge code from the AI System Design lab takes a `GatewayProvider`, an embedding provider
and an `IdentityProvider`. These three classes expose the same surface over HTTP, so that code runs unchanged
once the gateway and the identity provider become separate services.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional, Sequence

import httpx
from requisite.core.context import current_context
from requisite.core.exceptions import CostLimitException, ProviderException
from requisite.core.interfaces import ChatResponse, Message
from requisite.providers.base import BaseProvider
from requisite.rag.base import BaseEmbeddingProvider

from deploylab.config import Secret
from deploylab.serde import decode_response, encode_messages, encode_tools
from gateway.provider import GatewayExhausted
from identity.tokens import AuthError

NAME = "remote-gateway"


@dataclass(frozen=True)
class RemoteRoute:
    name: str
    tier: str


class RemoteGatewayProvider(BaseProvider):
    """A `BaseProvider` that sends every call to the gateway service.

    Failures keep their meaning: an exhausted gateway raises `GatewayExhausted`, a spent budget raises
    `CostLimitException`, so the callers' existing `except` clauses (hold the ticket, fail closed) still work.
    It also keeps a bounded list of the gateway's per-request audit facts, which the ticket platform and the
    knowledge assistant read to notice that a degraded-tier model served a request.
    """

    def __init__(self, base_url: str, service: str, service_key: Secret, timeout: float = 150.0) -> None:
        super().__init__(api_key="remote", model=NAME, timeout=timeout)
        self.base_url, self.service, self._key = base_url.rstrip("/"), service, service_key
        self.audit = SimpleNamespace(records=deque(maxlen=2000))
        self._routes: dict[str, RemoteRoute] = {}
        self._lock = threading.Lock()
        self._sync = httpx.Client(timeout=timeout)

    @property
    def name(self) -> str:
        return NAME

    @property
    def routes(self) -> list[RemoteRoute]:
        with self._lock:
            return list(self._routes.values())

    def validate_config(self) -> None:
        return None

    # -- request / response -------------------------------------------------------------------
    def _headers(self) -> dict:
        ctx = current_context()
        headers = {"X-Service": self.service, "X-Service-Key": self._key.reveal()}
        if ctx is not None:
            if ctx.correlation_id:
                headers["X-Correlation-Id"] = ctx.correlation_id
            if ctx.tenant:
                headers["X-Tenant"] = ctx.tenant
            if ctx.user:
                headers["X-Operator"] = ctx.user
        return headers

    @staticmethod
    def _body(messages, model, temperature, tools, kwargs) -> dict:
        extra = {k: kwargs[k] for k in ("tier", "no_cache") if k in kwargs}
        return {"messages": encode_messages(messages), "tools": encode_tools(tools), "temperature": temperature,
                "model": model, "options": extra}

    def _finish(self, status: int, payload: Any) -> ChatResponse:
        if status == 200:
            meta = payload.get("audit", {})
            with self._lock:
                if meta.get("route"):
                    self._routes[meta["route"]] = RemoteRoute(meta["route"], meta.get("tier", ""))
            self.audit.records.append(SimpleNamespace(**{"correlation_id": meta.get("correlation_id", ""),
                                                         "route": meta.get("route"), "outcome": meta.get("outcome", "")}))
            return decode_response(payload["response"])
        detail = payload.get("detail", "") if isinstance(payload, dict) else str(payload)
        kind = payload.get("error", "") if isinstance(payload, dict) else ""
        if status == 402 or kind == "budget":
            raise CostLimitException(detail or "budget exhausted")
        if status == 503 and kind in ("exhausted", ""):
            raise GatewayExhausted(detail or "gateway has no route available", provider=NAME)
        if status in (401, 403):
            raise ProviderException("the gateway rejected this service's credential", provider=NAME)
        raise ProviderException(f"gateway error {status}: {detail}"[:300], provider=NAME)

    def _transport_error(self, exc: Exception) -> ProviderException:
        return ProviderException(f"gateway unreachable: {type(exc).__name__}", provider=NAME, original_error=exc)

    def chat(self, messages: Sequence[Message], *, model: Optional[str] = None, temperature: Optional[float] = None,
             tools=None, response_model=None, **kwargs: Any) -> ChatResponse:
        if response_model is not None:
            raise ProviderException("structured output (response_model) is not exposed by the gateway service", provider=NAME)
        try:
            r = self._sync.post(f"{self.base_url}/v1/chat", json=self._body(messages, model, temperature, tools, kwargs),
                                headers=self._headers())
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc
        return self._finish(r.status_code, r.json() if r.content else {})

    async def achat(self, messages: Sequence[Message], *, model: Optional[str] = None, temperature: Optional[float] = None,
                    tools=None, response_model=None, **kwargs: Any) -> ChatResponse:
        if response_model is not None:
            raise ProviderException("structured output (response_model) is not exposed by the gateway service", provider=NAME)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(f"{self.base_url}/v1/chat", json=self._body(messages, model, temperature, tools, kwargs),
                                      headers=self._headers())
        except httpx.TransportError as exc:
            raise self._transport_error(exc) from exc
        return self._finish(r.status_code, r.json() if r.content else {})

    def stream(self, *args: Any, **kwargs: Any):
        raise ProviderException("streaming is not exposed by the gateway service", provider=NAME)

    def astream(self, *args: Any, **kwargs: Any):
        raise ProviderException("streaming is not exposed by the gateway service", provider=NAME)

    # -- readiness ----------------------------------------------------------------------------
    async def is_ready(self) -> bool:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.get(f"{self.base_url}/readyz")
        return r.status_code == 200

    def close(self) -> None:
        self._sync.close()


class RemoteEmbeddingProvider(BaseEmbeddingProvider):
    """Embeddings through the gateway service, so only the gateway holds the model provider's API key."""

    def __init__(self, base_url: str, service: str, service_key: Secret, batch: int = 64, timeout: float = 60.0) -> None:
        self.base_url, self.service, self._key, self.batch = base_url.rstrip("/"), service, service_key, batch
        self._client = httpx.Client(timeout=timeout)

    @property
    def name(self) -> str:
        return "remote-embeddings"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        ctx = current_context()
        headers = {"X-Service": self.service, "X-Service-Key": self._key.reveal()}
        if ctx is not None and ctx.correlation_id:
            headers["X-Correlation-Id"] = ctx.correlation_id
        for i in range(0, len(texts), self.batch):
            try:
                r = self._client.post(f"{self.base_url}/v1/embed", json={"texts": list(texts[i:i + self.batch])}, headers=headers)
            except httpx.TransportError as exc:
                raise ProviderException(f"gateway unreachable: {type(exc).__name__}", provider=NAME) from exc
            if r.status_code != 200:
                raise ProviderException(f"embedding request failed: {r.status_code}", provider=NAME)
            out.extend(r.json()["vectors"])
        return out

    async def aembed(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self.embed, texts)


class RemoteIdentity:
    """The login and token-exchange calls of the identity service, with the in-process `IdentityProvider` surface."""

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def _post(self, path: str, body: dict) -> dict:
        ctx = current_context()
        headers = {"X-Correlation-Id": ctx.correlation_id} if ctx is not None and ctx.correlation_id else {}
        try:
            r = self._client.post(f"{self.base_url}{path}", json=body, headers=headers)
        except httpx.TransportError as exc:
            raise AuthError("identity_unavailable") from exc
        if r.status_code != 200:
            reason = (r.json().get("reason") if r.content else None) or f"http_{r.status_code}"
            raise AuthError(reason)
        return r.json()

    def login(self, username: str, audience: str) -> str:
        return self._post("/login", {"username": username, "audience": audience})["token"]

    def exchange(self, token: str, calling_service: str, target_audience: str) -> str:
        return self._post("/exchange", {"token": token, "calling_service": calling_service,
                                        "target_audience": target_audience})["token"]
