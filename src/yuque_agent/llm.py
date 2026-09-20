"""LLM 客户端：OpenAI 兼容的 ``/chat/completions`` + function calling。

只做三件事：发请求、解析 tool_calls、暴露 usage。重试只针对**网络与 5xx**，
参数错误（400/401/422）直接抛出——那是我写错了，不该被静默重试掩盖。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx


class LLMError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_raw: str

    def arguments(self) -> dict[str, Any]:
        """解析参数。LLM 偶尔给出坏 JSON，此时返回带 ``__parse_error__`` 的字典。"""
        text = (self.arguments_raw or "").strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return {"__parse_error__": f"参数不是合法 JSON：{exc}", "__raw__": text[:500]}
        return parsed if isinstance(parsed, dict) else {"__parse_error__": "参数必须是 JSON 对象"}


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.cached_tokens += other.cached_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "in": self.prompt_tokens,
            "out": self.completion_tokens,
            "total": self.total_tokens,
            "cached": self.cached_tokens,
        }


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 300.0,
        max_retries: int = 3,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        send_reasoning_back: bool = True,
    ) -> None:
        if not api_key:
            raise LLMError("缺少 LLM API key")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens
        # DeepSeek 的 compat 标记要求把上一轮的 reasoning_content 原样回传
        self.send_reasoning_back = send_reasoning_back
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 主入口 -----------------------------------------------------------
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.HTTPError as exc:
                last_error = LLMError(f"网络错误（{type(exc).__name__}）：{exc}")
                self._backoff(attempt)
                continue

            if resp.status_code >= 500:
                last_error = LLMError(f"服务端 {resp.status_code}：{resp.text[:200]}")
                self._backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise LLMError(
                    f"请求被拒 {resp.status_code}：{resp.text[:500]}", status=resp.status_code
                )

            return self._parse(resp.json())

        raise last_error or LLMError("LLM 调用失败")

    def _parse(self, payload: dict[str, Any]) -> LLMResponse:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices：{json.dumps(payload)[:300]}")
        message = choices[0].get("message") or {}
        usage_raw = payload.get("usage") or {}
        details = usage_raw.get("prompt_tokens_details") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            cached_tokens=int(details.get("cached_tokens") or 0),
        )
        calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments_raw=str(fn.get("arguments") or ""),
                )
            )
        return LLMResponse(
            content=str(message.get("content") or ""),
            reasoning=str(message.get("reasoning_content") or ""),
            tool_calls=calls,
            usage=usage,
            finish_reason=str(choices[0].get("finish_reason") or ""),
        )

    def _backoff(self, attempt: int) -> None:
        time.sleep(min(2.0 * attempt, 8.0))


def assistant_message(response: LLMResponse, *, send_reasoning: bool = True) -> dict[str, Any]:
    """把 LLM 的回复转成可以塞回 messages 的 assistant 条目。"""
    message: dict[str, Any] = {"role": "assistant", "content": response.content or ""}
    if send_reasoning and response.reasoning:
        message["reasoning_content"] = response.reasoning
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_raw or "{}"},
            }
            for call in response.tool_calls
        ]
    return message


def tool_message(call_id: str, payload: Any) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }
