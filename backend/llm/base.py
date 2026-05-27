"""
Base LLM provider contracts.
"""

from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    messages: list[Message]
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 2000
    timeout_seconds: int = 60
    response_format: Literal["text", "json_object"] = "text"
    extras: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class BaseLLMProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def supports_model(self, model: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError
