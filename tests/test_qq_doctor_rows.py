"""守卫：`yqa doctor` 必须把**致命**的配置问题露出来。

真机问题：服务器上 `notify.members` 与 `default_target` 都是空的，也就是
**任何通知都发不出去**（全都会堆进 `unrouted/`）。`QQBotConfig.problems()`
早就有一条告警说这件事，但它装在「配置体检」那一行里，而主 `yqa doctor`
只挑 `{凭证, 入站命令, 通知积压, 二维码}` 四行 —— 于是这个状态**在主诊断里
完全看不见**，而主 doctor 正是上线验收清单上的那一条。

教训：一个「知道了但不显示」的告警等于不知道。
"""

from __future__ import annotations

import json
from pathlib import Path

from yuque_agent.config import Settings
from yuque_agent.qqbot.cli import qq_doctor_rows


def _settings(tmp_path: Path) -> Settings:
    settings = Settings(repo="g/kb", workspace=tmp_path / "ws")
    settings.ensure_dirs()
    return settings


def _write_config(settings: Settings, payload: dict) -> None:
    (settings.root / "qqbot.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_config_check_row_reaches_the_main_doctor(tmp_path: Path) -> None:
    """「配置体检」那一行必须在主 doctor 里出现。"""
    keys = [key for key, _ in qq_doctor_rows(_settings(tmp_path))]
    assert "配置体检" in keys, (
        "主 yqa doctor 没有显示「配置体检」——config.problems() 的告警就全被吞了。"
        f"实际显示的行：{keys}"
    )


def test_notifications_that_cannot_be_delivered_are_visible(tmp_path: Path) -> None:
    """**任何通知都发不出去**这种状态，必须能从主 doctor 一眼看到。

    这是真机上的实际配置（notify.members 空 + 没有兜底目标），
    后果是社员收不到任何 QQ，而 outbox 上看起来一切正常。
    """
    settings = _settings(tmp_path)
    _write_config(
        settings,
        {
            "version": 1,
            "notify": {"unmapped": "skip", "default_target": None, "members": {}},
            "inbound": {"enabled": True, "allow": ["someone"]},
        },
    )
    rows = dict(qq_doctor_rows(settings))
    blob = " ".join(rows.values())
    assert "无法投递" in blob or "members" in blob, f"主 doctor 里看不出「通知发不出去」：{rows}"


def test_rows_survive_a_broken_config_file(tmp_path: Path) -> None:
    """配置文件坏掉时 doctor 不能崩（它得能报出问题，而不是跟着一起死）。"""
    settings = _settings(tmp_path)
    (settings.root / "qqbot.json").write_text("{ not json", encoding="utf-8")
    rows = qq_doctor_rows(settings)
    assert rows, "配置坏了就什么都不显示？那正好是最需要看 doctor 的时候"
