"""所有模块共享的数据结构；不依赖某一家模型 SDK。"""
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol
import uuid


@dataclass
class ToolCall:
    name: str
    arguments: Dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:16])

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, data: dict) -> "ToolCall":
        return cls(name=data["name"], arguments=data["arguments"], id=data["id"])


@dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None

    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content,
                "tool_calls": [call.to_dict() for call in self.tool_calls],
                "tool_call_id": self.tool_call_id}

    @classmethod
    def from_dict(cls, data: dict) -> "Message":
        return cls(role=data["role"], content=data.get("content", ""),
                   tool_calls=[ToolCall.from_dict(x) for x in data.get("tool_calls", [])],
                   tool_call_id=data.get("tool_call_id"))


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ModelResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class Provider(Protocol):
    async def complete(self, messages: List[Message], tools: List[dict]) -> ModelResponse:
        ...


class StreamingProvider(Provider, Protocol):
    """可选扩展：逐片交付正文，工具参数在完整响应中返回，不能提前执行。"""

    supports_tool_streaming: bool

    async def stream(self, messages: List[Message], on_delta: Optional[Callable[[str], Any]],
                     tools: Optional[List[dict]] = None) -> ModelResponse:
        ...
