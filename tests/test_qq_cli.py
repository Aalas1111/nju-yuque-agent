"""``yqa qq …`` 子命令冒烟测试（只碰工作区，不联网、不需要凭证）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.qq_fakes import make_notice
from yuque_agent.config import Settings
from yuque_agent.qqbot.cli import qq_app
from yuque_agent.qqbot.config import QQBotConfig

runner = CliRunner()

CLEAN_ENV = {
    "YQA_QQ_APPID": "",
    "YQA_QQ_SECRET": "",
    "YQA_QQ_ACCOUNT": "",
    "YQA_QQ_CREDENTIALS": "",
    "YQA_QQ_CONFIG": "",
}


def _env_only_yuque_token(explicit: str = "") -> str:
    """只认环境变量，不读 ~/.yuque/auth.json（测试要可复现）。"""
    return explicit or os.environ.get("YQA_TOKEN", "") or os.environ.get("YUQUE_TOKEN", "")


def _env_only_llm_key(explicit: str = "") -> str:
    return explicit or os.environ.get("YQA_LLM_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")


@pytest.fixture(autouse=True)
def _no_ambient_qq_credentials(monkeypatch, tmp_path):
    """别让开发机上真实的 QQBot / 语雀凭证影响测试。"""
    for key in CLEAN_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YQA_QQ_CREDENTIALS", str(tmp_path / "creds.json"))
    # ~/.yuque/auth.json 与 ~/.pi/agent/auth.json 也不读，测试只认环境变量
    monkeypatch.setattr("yuque_agent.config.resolve_yuque_token", _env_only_yuque_token)
    monkeypatch.setattr("yuque_agent.config.resolve_llm_key", _env_only_llm_key)


def workspace_settings(tmp_path: Path) -> Settings:
    return Settings(repo="g/kb", workspace=tmp_path / "ws")


def test_config_init_creates_template(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app, ["config", "--init", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"]
    )
    assert result.exit_code == 0, result.output
    path = workspace_settings(tmp_path).root / "qqbot.json"
    assert path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["inbound"]["allow"] == []


def test_config_show_prints_json(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app, ["config", "--show", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"]
    )
    assert result.exit_code == 0, result.output
    assert '"inbound"' in result.output


def test_status_json_reports_unbound(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app,
        ["status", "--json", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["bound"] is False
    assert payload["notify"]["pending"] == 0
    assert any("未绑定" in value for _key, value in payload["rows"])


def _write_config(settings: Settings, payload: dict) -> Path:
    path = settings.root / "qqbot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_status_and_doctor_survive_exactly_one_problem(tmp_path: Path) -> None:
    """回归：正好 1 条体检问题时曾崩。

    ``"[yellow]；".join(problems) + "[/yellow]"`` 只在 problems ≥ 2 时「碰巧」有开标签，
    正好 1 条时 rich 抛 ``MarkupError: closing tag '[/yellow]' doesn't match any open tag``。
    而「只有 1 条问题」恰恰是最常见的状态（配了 unmapped 但还没填成员映射）。
    """
    settings = workspace_settings(tmp_path)
    path = _write_config(
        settings,
        {
            "version": 1,
            "notify": {"unmapped": "skip", "default_target": None, "members": {}},
            # 入站配好（否则「谁都不能用命令」也会进体检，就不止 1 条了）
            "inbound": {"enabled": True, "allow": ["u-1"], "admins": ["u-1"]},
        },
    )
    assert len(QQBotConfig.load(path).problems()) == 1  # 先锁住触发条件

    for command in ("status", "doctor"):
        result = runner.invoke(
            qq_app,
            [command, "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"],
            env=CLEAN_ENV,
        )
        assert result.exit_code == 0, result.output
        assert "MarkupError" not in result.output
        assert "通知无法投递" in result.output  # 那条问题照样显示出来


def test_yqa_doctor_survives_one_problem(tmp_path: Path) -> None:
    """主 CLI 的 doctor 也会渲染同一行（qq_doctor_rows）。"""
    from yuque_agent.cli import app as main_app

    settings = workspace_settings(tmp_path)
    _write_config(
        settings,
        {
            "version": 1,
            "notify": {"unmapped": "skip", "default_target": None, "members": {}},
            "inbound": {"enabled": True, "allow": ["u-1"], "admins": ["u-1"]},
        },
    )
    result = runner.invoke(
        main_app,
        ["doctor", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    assert "MarkupError" not in result.output
    # 主 doctor 只挑 4 行显示（凭证/入站/通知积压/二维码），不含「配置体检」——
    # 那条由 qq status / qq doctor 覆盖，这里只确认它不会把整个 doctor 带崩。
    assert "通知积压" in result.output


def test_status_shows_no_problem_when_config_is_complete(tmp_path: Path) -> None:
    settings = workspace_settings(tmp_path)
    _write_config(
        settings,
        {
            "version": 1,
            "notify": {
                "unmapped": "default",
                "default_target": {"scope": "group", "targetId": "g-1"},
                "members": {"张三": {"scope": "c2c", "targetId": "u-1"}},
            },
            "inbound": {
                "enabled": True,
                "allow": ["u-1"],
                "admins": ["u-1"],
                "user_groups": ["g-1"],
            },
        },
    )
    result = runner.invoke(
        qq_app, ["status", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"], env=CLEAN_ENV
    )
    assert result.exit_code == 0, result.output
    assert "没问题" in result.output


def test_status_table_runs(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app, ["status", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"], env=CLEAN_ENV
    )
    assert result.exit_code == 0, result.output
    assert "QQBot 状态" in result.output


def test_doctor_runs_without_credentials(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app, ["doctor", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"], env=CLEAN_ENV
    )
    assert result.exit_code == 0, result.output
    assert "自检" in result.output


def test_notify_dry_run_delivers_nothing(tmp_path: Path) -> None:
    settings = workspace_settings(tmp_path)
    config = qq_manual_config(settings)
    assert config.exists()
    bridge_pending = settings.notify_dir / "pending"
    notice = make_notice(bridge_pending / "000001-rejected-abc.json", seq=1)

    result = runner.invoke(
        qq_app,
        [
            "notify",
            "--dry-run",
            "--workspace",
            str(tmp_path / "ws"),
            "--repo",
            "g/kb",
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.output
    # 日志前缀不能被 rich 的 markup 吃掉（那是排查问题时最有用的部分）
    assert "[qqbot:notify]" in result.output
    assert notice.exists()  # dry-run 不移动文件


def test_notify_without_credentials_fails_clearly(tmp_path: Path) -> None:
    settings = workspace_settings(tmp_path)
    qq_manual_config(settings)
    make_notice(settings.notify_dir / "pending" / "000001-a.json", seq=1)

    result = runner.invoke(
        qq_app, ["notify", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"], env=CLEAN_ENV
    )
    assert result.exit_code == 1
    assert "绑定" in result.output


def test_logout_without_credentials(tmp_path: Path) -> None:
    result = runner.invoke(qq_app, ["logout", "--yes"], env=CLEAN_ENV)
    assert result.exit_code == 0
    assert "没有本地凭证" in result.output


def test_login_saves_credentials_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """把协议层换成假的，跑完整的 `yqa qq login`：流程 → 落盘 → 提示。"""
    import yuque_agent.qqbot.cli as qq_cli
    from tests.qq_fakes import FakeProtocol, completed

    fake = FakeProtocol(polls=[completed(app_id="app-cli", secret="sec-cli", openid="u-cli")])
    monkeypatch.setattr(qq_cli, "QQBotProtocol", lambda env="production": fake)
    credentials = tmp_path / "creds.json"
    result = runner.invoke(
        qq_app,
        [
            "login",
            "--no-qr",
            "--poll",
            "0",
            "--credentials",
            str(credentials),
            "--workspace",
            str(tmp_path / "ws"),
            "--repo",
            "g/kb",
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 0, result.output
    assert "绑定成功" in result.output
    payload = json.loads(credentials.read_text(encoding="utf-8"))
    saved = payload["accounts"]["default"]
    assert saved["appId"] == "app-cli"
    assert saved["clientSecret"] == "sec-cli"
    assert saved["userOpenid"] == "u-cli"


def test_login_failure_exits_nonzero(tmp_path: Path, monkeypatch) -> None:
    import yuque_agent.qqbot.cli as qq_cli
    from tests.qq_fakes import FakeProtocol, pending

    fake = FakeProtocol(polls=[pending()], default_status=1)
    monkeypatch.setattr(qq_cli, "QQBotProtocol", lambda env="production": fake)

    result = runner.invoke(
        qq_app,
        [
            "login",
            "--no-qr",
            "--poll",
            "0",
            "--timeout",
            "0.01",
            "--max-refreshes",
            "0",
            "--credentials",
            str(tmp_path / "creds.json"),
            "--workspace",
            str(tmp_path / "ws"),
            "--repo",
            "g/kb",
        ],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "没有完成" in result.output or "放弃" in result.output


def test_help_lists_commands() -> None:
    result = runner.invoke(qq_app, ["--help"])
    assert result.exit_code == 0
    for name in ("login", "status", "notify", "serve", "doctor", "config"):
        assert name in result.output


def test_serve_help_documents_auto_login_optout() -> None:
    result = runner.invoke(qq_app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--no-login" in result.output


# ------------------------------------------------- 服务启动时的自动扫码登录


def test_auto_login_writes_credentials_when_interactive(tmp_path: Path, monkeypatch) -> None:
    """stdout 是终端 + 没有缓存凭证 → 自动扫码、落盘、继续拿客户端。"""
    import yuque_agent.qqbot.cli as qq_cli
    from tests.qq_fakes import FakeProtocol, completed

    credentials = tmp_path / "creds.json"
    fake = FakeProtocol(polls=[completed(app_id="auto-app", secret="auto-sec", openid="u-auto")])
    monkeypatch.setattr(qq_cli, "QQBotProtocol", lambda env="production": fake)
    monkeypatch.setattr(qq_cli, "_interactive", lambda: True)

    sender, protocol = qq_cli.make_sender(
        credentials=credentials, login_if_needed=True, log=lambda _t: None
    )
    try:
        assert sender.app_id == "auto-app"  # type: ignore[attr-defined]
        saved = json.loads(credentials.read_text(encoding="utf-8"))["accounts"]["default"]
        assert saved["appId"] == "auto-app"
        assert saved["clientSecret"] == "auto-sec"
        assert fake.poll_count >= 1
    finally:
        if protocol is not None:
            protocol.close()


def test_auto_login_skipped_when_credentials_exist(tmp_path: Path, monkeypatch) -> None:
    """已经绑过就不该再弹二维码（用同一个凭证文件跑第二次）。"""
    import yuque_agent.qqbot.cli as qq_cli
    from tests.qq_fakes import FakeProtocol, completed

    credentials = tmp_path / "creds.json"
    qq_cli.CredentialStore(credentials).save(
        qq_cli.account_from_bind(app_id="cached-app", app_secret="cached-sec")
    )
    fake = FakeProtocol(polls=[completed()])
    monkeypatch.setattr(qq_cli, "QQBotProtocol", lambda env="production": fake)
    monkeypatch.setattr(qq_cli, "_interactive", lambda: True)

    sender, protocol = qq_cli.make_sender(
        credentials=credentials, login_if_needed=True, log=lambda _t: None
    )
    try:
        assert sender.app_id == "cached-app"  # type: ignore[attr-defined]
        assert fake.created == []  # 没创建绑定任务
    finally:
        if protocol is not None:
            protocol.close()


def test_auto_login_refuses_non_interactive(tmp_path: Path, monkeypatch) -> None:
    """systemd / cron 下不能傻等二维码——直接给可操作的报错。"""
    import yuque_agent.qqbot.cli as qq_cli
    from yuque_agent.qqbot.protocol import QQBotError

    monkeypatch.setattr(qq_cli, "_interactive", lambda: False)
    with pytest.raises(QQBotError, match="不是终端"):
        qq_cli.auto_login(credentials=tmp_path / "creds.json", log=lambda _t: None)


def test_auto_login_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    import yuque_agent.qqbot.cli as qq_cli
    from yuque_agent.qqbot.protocol import QQBotError

    monkeypatch.setattr(qq_cli, "_interactive", lambda: True)
    with pytest.raises(QQBotError, match="还没有绑定机器人"):
        qq_cli.make_sender(credentials=tmp_path / "creds.json", login_if_needed=False)


def test_serve_without_credentials_tells_user_to_scan(tmp_path: Path) -> None:
    """CLI 层确认：没凭证启动服务时会走扫码登录（这里 stdout 不是 tty，所以给出指引）。"""
    result = runner.invoke(
        qq_app,
        [
            "serve",
            "--no-watch",
            "--no-inbound",
            "--workspace",
            str(tmp_path / "ws"),
            "--repo",
            "g/kb",
        ],
        env={**CLEAN_ENV, "YQA_TOKEN": "dummy-token"},
    )
    assert result.exit_code == 1
    assert "扫码" in result.output or "二维码" in result.output


def test_serve_no_login_flag_errors_instead(tmp_path: Path) -> None:
    result = runner.invoke(
        qq_app,
        [
            "serve",
            "--no-login",
            "--no-watch",
            "--no-inbound",
            "--workspace",
            str(tmp_path / "ws"),
            "--repo",
            "g/kb",
        ],
        env={**CLEAN_ENV, "YQA_TOKEN": "dummy-token"},
    )
    assert result.exit_code == 1
    assert "还没有绑定机器人" in result.output


def test_serve_checks_yuque_token_before_qr(tmp_path: Path) -> None:
    """缺语雀 token 时先报错，别让人白扫一次码。"""
    result = runner.invoke(
        qq_app,
        ["serve", "--no-watch", "--no-inbound", "--workspace", str(tmp_path / "ws")],
        env=CLEAN_ENV,
    )
    assert result.exit_code == 1
    assert "语雀 token" in result.output


def qq_manual_config(settings: Settings) -> Path:
    """给测试造一份最小可用的 qqbot.json。"""
    path = settings.root / "qqbot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "notify": {
                    "unmapped": "default",
                    "default_target": {"scope": "group", "targetId": "g-1"},
                    "members": {"张三": {"scope": "c2c", "targetId": "u-1"}},
                },
                "inbound": {"enabled": True, "allow": ["u-1"], "admins": ["u-1"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path
