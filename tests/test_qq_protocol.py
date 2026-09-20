"""协议层测试：扫码绑定 / access_token / REST。**全部离线。**"""

from __future__ import annotations

import base64
import json

import pytest

from tests.qq_fakes import BIND_KEY, FakeResponse, FakeTransport, encrypt_secret, retcode
from yuque_agent.qqbot.protocol import (
    ApiError,
    BindStatus,
    QQBotError,
    QQBotProtocol,
    bind_status_name,
    build_connect_url,
    decrypt_secret,
    generate_bind_key,
    get_qqbot_host,
)


def test_generate_bind_key_is_32_random_bytes() -> None:
    first = generate_bind_key()
    second = generate_bind_key()
    assert first != second
    assert len(base64.b64decode(first)) == 32


def test_host_selection() -> None:
    assert get_qqbot_host("production") == "q.qq.com"
    assert get_qqbot_host("test") == "test.q.qq.com"
    assert get_qqbot_host("nonsense") == "q.qq.com"


def test_build_connect_url_carries_task_and_source() -> None:
    url = build_connect_url("task-1", "yuque-agent")
    assert url.startswith("https://q.qq.com/qqbot/openclaw/connect.html?")
    assert "task_id=task-1" in url
    assert "source=yuque-agent" in url
    assert "_wv=2" in url


def test_decrypt_secret_roundtrip() -> None:
    encrypted = encrypt_secret("my-app-secret")
    assert decrypt_secret(encrypted, BIND_KEY) == "my-app-secret"


def test_decrypt_secret_rejects_bad_key_length() -> None:
    encrypted = encrypt_secret("x")
    with pytest.raises(QQBotError, match="32 字节"):
        decrypt_secret(encrypted, base64.b64encode(b"short").decode())


def test_decrypt_secret_rejects_garbage() -> None:
    with pytest.raises(QQBotError, match="base64|解密失败|长度不足"):
        decrypt_secret("!!!not-base64!!!", BIND_KEY)


def test_bind_status_name() -> None:
    assert bind_status_name(2) == "completed"
    assert bind_status_name(99) == "unknown(99)"


def test_create_bind_task_parses_task_id() -> None:
    transport = FakeTransport([retcode({"task_id": "t-123"})])
    protocol = QQBotProtocol(transport=transport)
    task = protocol.create_bind_task()
    assert task.task_id == "t-123"
    assert len(base64.b64decode(task.key)) == 32
    request = transport.last()
    assert request["url"] == "https://q.qq.com/lite/create_bind_task"
    assert request["json"] == {"key": task.key}


def test_create_bind_task_raises_on_retcode() -> None:
    transport = FakeTransport([retcode({}, retcode_value=1001, msg="bad key")])
    with pytest.raises(QQBotError, match="1001"):
        QQBotProtocol(transport=transport).create_bind_task()


def test_create_bind_task_raises_without_task_id() -> None:
    transport = FakeTransport([retcode({})])
    with pytest.raises(QQBotError, match="task_id"):
        QQBotProtocol(transport=transport).create_bind_task()


def test_poll_bind_result_maps_fields() -> None:
    transport = FakeTransport(
        [
            retcode(
                {
                    "status": 2,
                    "bot_appid": 102000001,
                    "bot_encrypt_secret": "abc",
                    "openid": "u-9",
                }
            )
        ]
    )
    result = QQBotProtocol(transport=transport).poll_bind_result("t-1")
    assert result.status == int(BindStatus.COMPLETED)
    assert result.completed and not result.expired
    assert result.bot_app_id == "102000001"
    assert result.bot_encrypt_secret == "abc"
    assert result.user_openid == "u-9"


def test_poll_bind_result_defaults_to_none_status() -> None:
    transport = FakeTransport([retcode({})])
    result = QQBotProtocol(transport=transport).poll_bind_result("t-1")
    assert result.status == int(BindStatus.NONE)
    assert not result.completed


def test_get_access_token() -> None:
    transport = FakeTransport([FakeResponse({"access_token": "tok", "expires_in": 7200})])
    protocol = QQBotProtocol(transport=transport, token_base="https://bots.qq.com")
    token = protocol.get_access_token("app", "sec")
    assert token.access_token == "tok"
    assert token.expires_in == 7200
    request = transport.last()
    assert request["url"] == "https://bots.qq.com/app/getAppAccessToken"
    assert request["json"] == {"appId": "app", "clientSecret": "sec"}


def test_get_access_token_requires_credentials() -> None:
    with pytest.raises(QQBotError, match="appId"):
        QQBotProtocol(transport=FakeTransport()).get_access_token("", "")


def test_get_access_token_surfaces_http_error() -> None:
    transport = FakeTransport([FakeResponse({"error": "bad"}, status_code=401)])
    with pytest.raises(ApiError, match="鉴权失败"):
        QQBotProtocol(transport=transport).get_access_token("app", "sec")


def test_api_request_injects_bot_authorization() -> None:
    transport = FakeTransport([FakeResponse({"id": "m-1"})])
    protocol = QQBotProtocol(transport=transport, api_base="https://api.sgroup.qq.com")
    data = protocol.api_request("tok", "POST", "v2/users/u-1/messages", {"content": "hi"})
    assert data == {"id": "m-1"}
    request = transport.last()
    assert request["url"] == "https://api.sgroup.qq.com/v2/users/u-1/messages"
    assert request["headers"]["Authorization"] == "QQBot tok"


def test_api_request_detects_html_error_page() -> None:
    transport = FakeTransport(
        [FakeResponse(raw_text="<html>502 Bad Gateway</html>", status_code=502)]
    )
    with pytest.raises(ApiError):
        QQBotProtocol(transport=transport).api_request("tok", "GET", "/gateway")


def test_api_request_rejects_non_json_body() -> None:
    transport = FakeTransport([FakeResponse(raw_text="not json at all")])
    with pytest.raises(ApiError, match="JSON"):
        QQBotProtocol(transport=transport).api_request("tok", "GET", "/gateway")


def test_protocol_does_not_close_injected_transport() -> None:
    transport = FakeTransport()
    protocol = QQBotProtocol(transport=transport)
    protocol.close()
    assert transport.closed is False


def test_protocol_closes_its_own_transport() -> None:
    transport = FakeTransport()
    protocol = QQBotProtocol(transport=transport)
    protocol._owns_transport = True
    protocol.close()
    assert transport.closed is True


def test_bind_result_repr_hides_secret() -> None:
    transport = FakeTransport([retcode({"status": 2, "bot_encrypt_secret": "super-secret"})])
    result = QQBotProtocol(transport=transport).poll_bind_result("t-1")
    assert "super-secret" not in repr(result)
    assert json.dumps(result.bot_app_id) == '""'
