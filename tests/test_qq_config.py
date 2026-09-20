"""qqbot.json 配置测试：成员映射、兜底、默认拒绝、体检。"""

from __future__ import annotations

import json
from pathlib import Path

from yuque_agent.config import Settings
from yuque_agent.qqbot.config import (
    NotifyTarget,
    QQBotConfig,
    default_config_path,
    init_config,
    load_config,
)


def test_missing_config_is_deny_by_default(tmp_path: Path) -> None:
    config = QQBotConfig.load(tmp_path / "nope.json")
    assert config.members == {}
    assert config.notify_default is None
    assert config.inbound_allow == ()
    assert config.is_allowed("anyone") is False
    assert config.is_admin("anyone") is False


def test_load_full_config(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": "yuque-agent",
                "notify": {
                    "unmapped": "skip",
                    "default_target": {"scope": "group", "targetId": "g-default"},
                    "members": {
                        "张三": {"scope": "c2c", "targetId": "u-zhang"},
                        "李四": {"openid": "u-li"},
                    },
                },
                "inbound": {
                    "enabled": True,
                    "allow": ["u-zhang"],
                    "admins": ["u-zhang"],
                    "groups": ["g-1"],
                    "rate_limit_seconds": 5,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = QQBotConfig.load(path)
    assert config.notify_unmapped == "skip"
    assert config.notify_default == NotifyTarget("group", "g-default")
    assert config.members["张三"].target.to_str() == "c2c:u-zhang"
    assert config.members["李四"].scope == "c2c"  # openid 简写默认私聊
    assert config.inbound_allow == ("u-zhang",)
    assert config.inbound_rate_limit == 5


def test_resolve_member_prefers_exact_then_case_insensitive() -> None:
    config = QQBotConfig(members={"Zhang San": NotifyTarget("c2c", "u-1")})
    target, basis = config.resolve_member("Zhang San")
    assert basis == "member" and target is not None
    target, basis = config.resolve_member("zhang san")
    assert basis == "member" and target is not None


def test_resolve_member_falls_back_to_default_or_skip() -> None:
    with_default = QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"), notify_unmapped="default"
    )
    target, basis = with_default.resolve_member("查无此人")
    assert basis == "default" and target is not None and target.target_id == "g-1"

    skipping = QQBotConfig(notify_default=NotifyTarget("group", "g-1"), notify_unmapped="skip")
    target, basis = skipping.resolve_member("查无此人")
    assert basis == "skip" and target is None

    neither = QQBotConfig()
    target, basis = neither.resolve_member("")
    assert basis == "unmapped" and target is None


def test_inbound_allowlist_and_group_gate() -> None:
    config = QQBotConfig(inbound_allow=("u-1",), inbound_admins=("u-1",), inbound_groups=("g-1",))
    assert config.is_allowed("u-1") is True
    assert config.is_allowed("u-2") is False
    assert config.is_allowed("u-1", "g-1") is True
    assert config.is_allowed("u-1", "g-2") is False  # 群不在名单里
    assert config.is_admin("u-1") is True
    assert config.is_admin("u-2") is False


def test_inbound_disabled_rejects_everyone() -> None:
    config = QQBotConfig(inbound_enabled=False, inbound_allow=("u-1",))
    assert config.is_allowed("u-1") is False


def test_problems_reports_dangerous_misconfiguration() -> None:
    bad = QQBotConfig(inbound_admins=("u-1",))
    problems = bad.problems()
    assert any("默认拒绝" in item for item in problems)

    ok = QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"),
        members={"张三": NotifyTarget("c2c", "u-1")},
        inbound_allow=("u-1",),
        inbound_admins=("u-1",),
    )
    assert ok.problems() == []


def test_roundtrip_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    config = QQBotConfig(
        notify_default=NotifyTarget("group", "g-1"),
        members={"张三": NotifyTarget("c2c", "u-1", note="部长")},
        notify_unmapped="skip",
        inbound_allow=("u-1",),
        inbound_admins=("u-1",),
    )
    config.save(path)
    reloaded = QQBotConfig.load(path)
    assert reloaded.members["张三"].note == "部长"
    assert reloaded.notify_unmapped == "skip"
    assert reloaded.inbound_admins == ("u-1",)


def test_bad_config_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    path.write_text("[1,2,3]", encoding="utf-8")
    assert QQBotConfig.load(path).members == {}
    path.write_text("{broken", encoding="utf-8")
    assert QQBotConfig.load(path).members == {}


def test_bad_unmapped_value_falls_back_to_default(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    path.write_text(json.dumps({"notify": {"unmapped": "whatever"}}), encoding="utf-8")
    assert QQBotConfig.load(path).notify_unmapped == "default"


def test_default_config_path_follows_workspace(tmp_path: Path) -> None:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    assert default_config_path(settings) == tmp_path / "ws" / "g_kb" / "qqbot.json"
    assert load_config(settings).path == default_config_path(settings)


def test_init_config_creates_template_once(tmp_path: Path) -> None:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    path = init_config(settings)
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["inbound"]["allow"] == []
    first = path.read_text(encoding="utf-8")
    init_config(settings)  # 第二次不应覆盖
    assert path.read_text(encoding="utf-8") == first


def test_describe_is_human_readable(tmp_path: Path) -> None:
    config = QQBotConfig(members={"张三": NotifyTarget("c2c", "u-1")}, path=tmp_path / "q.json")
    text = config.describe()
    assert "成员映射 1 条" in text
    assert "白名单 0 人" in text
