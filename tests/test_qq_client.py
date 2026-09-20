"""消息客户端测试：目标解析、发送体、token 缓存、dry-run。"""

from __future__ import annotations

import pytest

from tests.qq_fakes import FakeResponse, FakeTransport
from yuque_agent.qqbot.client import NullSender, QQBotClient, Target, next_msg_seq
from yuque_agent.qqbot.credentials import QQBotAccount
from yuque_agent.qqbot.protocol import QQBotError, QQBotProtocol


def make_client(**kwargs) -> tuple[QQBotClient, FakeTransport]:
    counter = {"token": 0, "msg": 0}

    def handler(request):  # noqa: ANN001
        if "getAppAccessToken" in request["url"]:
            counter["token"] += 1
            return FakeResponse({"access_token": f"tok-{counter['token']}", "expires_in": 7200})
        counter["msg"] += 1
        return FakeResponse({"id": f"m-{counter['msg']}"})

    transport = FakeTransport(handler=handler)
    account = QQBotAccount(app_id="102000001", app_secret="sek")
    kwargs.setdefault("markdown_support", False)
    client = QQBotClient(account, protocol=QQBotProtocol(transport=transport), **kwargs)
    return client, transport


# ---------------------------------------------------------------- Target


def test_target_parse_variants() -> None:
    assert Target.parse("c2c:openid-1").scope == "c2c"
    assert Target.parse("group:group-1").target_id == "group-1"
    assert Target.parse("openid-2").scope == "c2c"  # 只给 id 默认私聊
    assert Target.parse("群:g-1").scope == "group"
    assert Target.parse("c2c:o", msg_id="m-9").msg_id == "m-9"


def test_target_paths() -> None:
    assert Target("c2c", "u1").path == "/v2/users/u1/messages"
    assert Target("group", "g1").path == "/v2/groups/g1/messages"
    assert Target("group", "g1").to_str() == "group:g1"


def test_target_rejects_bad_scope_and_empty_id() -> None:
    with pytest.raises(ValueError):
        Target("channel", "x")
    with pytest.raises(ValueError):
        Target("c2c", "")
    with pytest.raises(ValueError):
        Target.parse("   ")


def test_next_msg_seq_in_range() -> None:
    assert all(0 <= next_msg_seq() < 65536 for _ in range(20))


# ---------------------------------------------------------------- 发送


def test_send_text_proactive_body() -> None:
    client, transport = make_client()
    client.send_text(Target("c2c", "u-1"), "你好")
    body = transport.last()["json"]
    assert body == {"content": "你好", "msg_type": 0}
    assert transport.last()["url"].endswith("/v2/users/u-1/messages")
    assert transport.last()["headers"]["Authorization"] == "QQBot tok-1"


def test_send_text_passive_reply_carries_msg_id_and_seq() -> None:
    client, transport = make_client()
    client.send_text(Target("group", "g-1", msg_id="msg-9"), "收到")
    body = transport.last()["json"]
    assert body["msg_id"] == "msg-9"
    assert 0 <= body["msg_seq"] < 65536
    assert body["content"] == "收到"


def test_send_markdown_uses_msg_type_2() -> None:
    client, transport = make_client(markdown_support=True)
    client.send_text(Target("c2c", "u-1"), "**加粗**")
    body = transport.last()["json"]
    assert body["msg_type"] == 2
    assert body["markdown"] == {"content": "**加粗**"}


def test_send_markdown_false_overrides_account_support() -> None:
    client, transport = make_client(markdown_support=True)
    client.send_text(Target("c2c", "u-1"), "plain", markdown=False)
    assert "content" in transport.last()["json"]


def test_send_raw_fills_msg_type() -> None:
    client, transport = make_client()
    client.send_raw(Target("c2c", "u-1"), {"media": {"file_info": "x"}})
    assert transport.last()["json"]["msg_type"] == 7


def test_send_rejects_empty_text() -> None:
    client, _ = make_client()
    with pytest.raises(QQBotError, match="空消息"):
        client.send_text(Target("c2c", "u-1"), "   ")


def test_send_to_accepts_string_target() -> None:
    client, transport = make_client()
    client.send_to("group:g-1", "hi")
    assert transport.last()["url"].endswith("/v2/groups/g-1/messages")


# ---------------------------------------------------------------- token


def test_access_token_is_cached_across_sends() -> None:
    client, transport = make_client()
    client.send_text(Target("c2c", "u-1"), "a")
    client.send_text(Target("c2c", "u-1"), "b")
    token_calls = [url for url in transport.urls() if "getAppAccessToken" in url]
    assert len(token_calls) == 1


def test_access_token_cache_can_be_cleared() -> None:
    client, transport = make_client()
    client.send_text(Target("c2c", "u-1"), "a")
    client.clear_token_cache()
    client.send_text(Target("c2c", "u-1"), "b")
    token_calls = [url for url in transport.urls() if "getAppAccessToken" in url]
    assert len(token_calls) == 2


def test_token_status_reports_valid() -> None:
    client, _ = make_client()
    assert client.token_status()["status"] == "none"
    client.get_access_token()
    status = client.token_status()
    assert status["status"] == "valid"
    assert status["remainingSeconds"] > 0


def test_gateway_url() -> None:
    transport = FakeTransport(
        [
            FakeResponse({"access_token": "tok", "expires_in": 7200}),
            FakeResponse({"url": "wss://api.sgroup.qq.com/websocket/"}),
        ]
    )
    client = QQBotClient(
        QQBotAccount(app_id="a", app_secret="s"), protocol=QQBotProtocol(transport=transport)
    )
    assert client.gateway_url().startswith("wss://")


def test_client_requires_credentials() -> None:
    with pytest.raises(QQBotError, match="AppID"):
        QQBotClient(QQBotAccount(app_id="only"))


def test_null_sender_records_without_network() -> None:
    sender = NullSender()
    sender.send_text(Target("c2c", "u-1"), "dry")
    assert sender.sent[0][1] == "dry"
    assert sender.get_access_token() == "(dry-run)"
