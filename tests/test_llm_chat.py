"""`LLMClient.chat` 的重试与解析（`ping` 那套在 test_llm_probe.py）。

为什么单独测：常驻进程整晚都在调它，而「网络抖一下」「服务端 5xx」和「key 过期 /
参数写错」的处理**必须不同**——前者该退避重试，后者重试三次只是白等三倍时间
（而且把「我写错了」掩盖成「网络问题」）。2026-09-27 复检发现这条路径没有任何测试，
改坏了只会静默地多花/少花重试，所以补上。
"""

from __future__ import annotations

import httpx
import pytest

from yuque_agent import llm
from yuque_agent.llm import LLMClient, LLMError, assistant_message, tool_message


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


class _SeqHTTP:
    """替掉 ``LLMClient._client``：按脚本逐个回（``Exception`` 就抛出来）。"""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.slept: list[float] = []
        """退避时长（真 sleep 会被 monkeypatch 成往这里追加）。"""

    def post(self, url: str, **kwargs: object) -> _Resp:
        self.calls.append({"url": url, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, _Resp)
        return item

    def close(self) -> None:
        pass


def _client(monkeypatch: pytest.MonkeyPatch, http: _SeqHTTP, **kwargs: object) -> LLMClient:
    client = LLMClient(
        base_url="https://api.example.com",
        api_key="sk-test",
        model="m",
        **kwargs,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(client, "_client", http)
    # 真退避会睡 2+4+6 秒；测试里只记不睡
    monkeypatch.setattr(llm.time, "sleep", http.slept.append)
    return client


def _body(content: str = "hello") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}


# ---------------------------------------------------------------- 重试


def test_network_error_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _SeqHTTP(httpx.ConnectError("抖了一下"), _Resp(200, _body()))
    client = _client(monkeypatch, http)

    response = client.chat([{"role": "user", "content": "hi"}])

    assert response.content == "hello"
    assert len(http.calls) == 2, "网络错误应当退避后重试一次"
    assert http.slept, "重试前必须等一下（退避）"


def test_server_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _SeqHTTP(_Resp(502, text="bad gateway"), _Resp(200, _body()))
    assert _client(monkeypatch, http).chat([]).content == "hello"
    assert len(http.calls) == 2


def test_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """400/401/422 是「我写错了」，直接抛——不该被静默重试掩盖，也不该白等退避。"""
    http = _SeqHTTP(_Resp(401, text="invalid key"))
    client = _client(monkeypatch, http)
    with pytest.raises(LLMError) as exc:
        client.chat([])
    assert exc.value.status == 401
    assert len(http.calls) == 1
    assert http.slept == []


def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _SeqHTTP(*[_Resp(500, text="boom") for _ in range(3)])
    with pytest.raises(LLMError):
        _client(monkeypatch, http, max_retries=3).chat([])
    assert len(http.calls) == 3


# ---------------------------------------------------------------- 解析


def test_parses_tool_calls_usage_and_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "reasoning_content": "先看看目录",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {"name": "kb_tree", "arguments": '{"a": 1}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 64},
        },
    }
    response = _client(monkeypatch, _SeqHTTP(_Resp(200, body))).chat([])

    assert response.wants_tools
    assert response.tool_calls[0].name == "kb_tree"
    assert response.tool_calls[0].arguments() == {"a": 1}
    assert response.reasoning == "先看看目录"
    assert response.finish_reason == "tool_calls"
    assert response.usage.cached_tokens == 64


def test_empty_choices_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LLMError):
        _client(monkeypatch, _SeqHTTP(_Resp(200, {"choices": []}))).chat([])


def test_tools_are_sent_only_when_given(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _SeqHTTP(_Resp(200, _body()), _Resp(200, _body()))
    client = _client(monkeypatch, http)
    client.chat([])
    client.chat([], tools=[{"type": "function", "function": {"name": "x"}}])
    assert "tools" not in http.calls[0]["json"]
    assert http.calls[1]["json"]["tool_choice"] == "auto"


def test_bad_arguments_do_not_crash_the_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "choices": [
            {
                "message": {
                    "tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "{半截"}}]
                }
            }
        ],
        "usage": {},
    }
    call = _client(monkeypatch, _SeqHTTP(_Resp(200, body))).chat([]).tool_calls[0]
    assert "__parse_error__" in call.arguments()


def test_assistant_message_echoes_reasoning_only_when_asked() -> None:
    """`send_reasoning_back` 是开关：DeepSeek 的 compat 标记要它原样回传，可以关掉。"""
    from yuque_agent.llm import LLMResponse, ToolCall

    response = LLMResponse(
        content="正文",
        reasoning="思考",
        tool_calls=[ToolCall(id="c", name="kb_tree", arguments_raw="{}")],
    )
    with_reasoning = assistant_message(response, send_reasoning=True)
    without = assistant_message(response, send_reasoning=False)
    assert with_reasoning["reasoning_content"] == "思考"
    assert "reasoning_content" not in without
    assert with_reasoning["tool_calls"][0]["function"]["name"] == "kb_tree"


def test_tool_message_is_json() -> None:
    import json

    message = tool_message("c1", {"ok": True, "result": {"中文": 1}})
    assert message["role"] == "tool" and message["tool_call_id"] == "c1"
    assert json.loads(message["content"])["result"]["中文"] == 1
