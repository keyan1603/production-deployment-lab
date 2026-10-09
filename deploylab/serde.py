"""Wire format between services: Requisite messages, tools and responses as plain JSON.

The one non-obvious part is `ToolCall.provider_data`: an opaque, provider-specific value that must come back
exactly as the provider produced it on the next turn (Gemini's thought signature is bytes). JSON cannot carry
bytes, so they travel as {"__bytes__": "<base64>"} and are restored on the other side.
"""

from __future__ import annotations

import base64
from enum import Enum
from typing import Any, Sequence

from pydantic import BaseModel
from requisite.core.interfaces import ChatResponse, Message, Usage
from requisite.tools.base import Tool

_BYTES = "__bytes__"


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, (bytes, bytearray)):
        return {_BYTES: base64.b64encode(bytes(obj)).decode("ascii")}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump(mode="python"))
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    return obj


def from_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj) == {_BYTES}:
            return base64.b64decode(obj[_BYTES])
        return {k: from_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_jsonable(v) for v in obj]
    return obj


def encode_messages(messages: Sequence[Message]) -> list[dict]:
    return [to_jsonable(m) for m in messages]


def decode_messages(items: list[dict]) -> list[Message]:
    return [Message(**from_jsonable(i)) for i in items]


def _unavailable(**_: Any) -> str:
    raise RuntimeError("tools are executed by the calling service, never by the gateway")


def encode_tools(tools: Sequence[Tool] | None) -> list[dict]:
    return [{"name": t.name, "description": t.description, "parameters_schema": t.parameters_schema}
            for t in (tools or [])]


def decode_tools(items: list[dict]) -> list[Tool]:
    """The gateway only needs each tool's schema to offer it to the model; running a tool stays with the caller."""
    return [Tool(name=i["name"], description=i.get("description", ""), parameters_schema=i.get("parameters_schema", {}),
                 func=_unavailable) for i in items]


def encode_response(resp: ChatResponse) -> dict:
    return to_jsonable({"content": resp.content, "model": resp.model, "provider": resp.provider,
                        "usage": resp.usage, "finish_reason": resp.finish_reason, "tool_calls": resp.tool_calls})


def decode_response(data: dict) -> ChatResponse:
    data = from_jsonable(data)
    return ChatResponse(content=data["content"], model=data["model"], provider=data["provider"],
                        usage=Usage(**data.get("usage", {})), finish_reason=data.get("finish_reason"),
                        tool_calls=data.get("tool_calls", []))
