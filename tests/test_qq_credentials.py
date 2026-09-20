"""凭证存取测试：落盘权限、优先级、绝不泄露密钥。"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

from yuque_agent.qqbot.credentials import (
    ENV_APP_ID,
    ENV_APP_SECRET,
    ENV_CREDENTIALS_PATH,
    CredentialStore,
    QQBotAccount,
    account_from_bind,
    credentials_path,
    mask_secret,
    resolve_account,
)
from yuque_agent.qqbot.protocol import QQBotError


def test_mask_secret_never_reveals_middle() -> None:
    assert mask_secret("") == "(空)"
    assert mask_secret("short") == "*****"
    masked = mask_secret("abcdefghijklmnop")
    assert masked.startswith("abcd") and "…" in masked
    assert "efghijklm" not in masked


def test_repr_does_not_contain_secret() -> None:
    account = QQBotAccount(app_id="102000001", app_secret="top-secret-value")
    assert "top-secret-value" not in repr(account)
    assert "top-secret-value" not in account.describe()


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "qqbot.json")
    account = account_from_bind(app_id="102000001", app_secret="sek", user_openid="u-1")
    path = store.save(account)
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["accounts"]["default"]["appId"] == "102000001"
    assert raw["accounts"]["default"]["clientSecret"] == "sek"

    loaded = store.get("default")
    assert loaded is not None
    assert loaded.app_id == "102000001"
    assert loaded.app_secret == "sek"
    assert loaded.user_openid == "u-1"
    assert loaded.bound_via == "qr"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Windows 没有 POSIX 权限位")
def test_credentials_file_is_0600(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "sub" / "qqbot.json")
    path = store.save(QQBotAccount(app_id="a", app_secret="b"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # 目录是我们新建的 → 不能给同机其他用户读
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0


def test_multiple_accounts_and_delete(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "qqbot.json")
    store.save(QQBotAccount(app_id="a1", app_secret="s1", account="default"))
    store.save(QQBotAccount(app_id="a2", app_secret="s2", account="second"))
    assert store.accounts() == ["default", "second"]
    assert store.delete("default") is True
    assert store.get("default") is None
    assert store.get("second") is not None
    assert store.delete("second") is True
    assert not store.path.exists()  # 最后一个账户删掉后文件也消失
    assert store.delete("second") is False


def test_save_refuses_incomplete_account(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "qqbot.json")
    with pytest.raises(QQBotError, match="不完整"):
        store.save(QQBotAccount(app_id="only-id"))


def test_load_tolerates_broken_file(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert CredentialStore(path).load() == {}


def test_load_accepts_legacy_flat_file(tmp_path: Path) -> None:
    path = tmp_path / "qqbot.json"
    path.write_text(json.dumps({"appId": "a", "appSecret": "s"}), encoding="utf-8")
    account = CredentialStore(path).get("default")
    assert account is not None and account.app_id == "a" and account.app_secret == "s"


def test_resolve_account_prefers_explicit_then_env_then_file(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "qqbot.json")
    store.save(QQBotAccount(app_id="file-id", app_secret="file-secret"))

    from_file = resolve_account(store=store, env={})
    assert (from_file.app_id, from_file.app_secret) == ("file-id", "file-secret")

    from_env = resolve_account(
        store=store, env={ENV_APP_ID: "env-id", ENV_APP_SECRET: "env-secret"}
    )
    assert (from_env.app_id, from_env.app_secret) == ("env-id", "env-secret")

    explicit = resolve_account(
        store=store,
        app_id="cli-id",
        app_secret="cli-secret",
        env={ENV_APP_ID: "env-id", ENV_APP_SECRET: "env-secret"},
    )
    assert (explicit.app_id, explicit.app_secret) == ("cli-id", "cli-secret")


def test_resolve_account_partial_explicit_falls_back(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "qqbot.json")
    store.save(QQBotAccount(app_id="file-id", app_secret="file-secret"))
    resolved = resolve_account(store=store, app_id="cli-id", env={})
    assert resolved.app_id == "cli-id"
    assert resolved.app_secret == "file-secret"


def test_resolve_account_without_anything_is_empty(tmp_path: Path) -> None:
    resolved = resolve_account(store=CredentialStore(tmp_path / "qqbot.json"), env={})
    assert resolved.complete is False
    assert "未绑定" in resolved.describe()


def test_credentials_path_env_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_CREDENTIALS_PATH, str(tmp_path / "custom.json"))
    assert credentials_path(None) == tmp_path / "custom.json"
    assert credentials_path(tmp_path / "explicit.json") == tmp_path / "explicit.json"


def test_describe_mentions_mask_not_secret() -> None:
    account = QQBotAccount(app_id="102000001", app_secret="abcdefghijkl", user_openid="u-1")
    text = account.describe()
    assert "102000001" in text
    assert "abcdefghijkl" not in text
