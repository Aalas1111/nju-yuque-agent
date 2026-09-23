"""守卫：`docs/deploy.md` 里的 systemd 单元必须和 `deploy/*.service` 一致。

为什么要有这条：单元**曾经漂过**——文档里少了 `Documentation=` 和
`SyslogIdentifier=` 两个真机上真有的指令，`ExecStart` 还是折成三行的写法。
照文档敲出来的单元和正在跑的不是同一个文件，而这种事**没人会去对账**。

现在仓库里有权威副本（`deploy/*.service`，直接从真机取的），文档里那份是给人读的。
两边只要不一致，这个测试就失败。

而且光有「一致性」还不够：**两边一起改错也还是一致**。
所以另加一层，把几件真出过事、一出事就难查的指令单独钉死。
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DOC = ROOT / "docs" / "deploy.md"

#: 仓库里的权威副本 → 它在 docs/deploy.md 里对应的章节号。
UNITS = {
    "yuque-agent.service": "4",
    "yuque-agent-plan.service": "10",
}


def _unit_block_in_doc(section_no: str) -> str:
    """把 `docs/deploy.md` 某个章节里的 ```ini 代码块抠出来。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    nxt = str(int(section_no) + 1)
    pattern = re.compile(
        rf"^## {re.escape(section_no)}\..*?(?=^## {nxt}\.|\Z)", re.S | re.M
    )
    section = pattern.search(text)
    assert section, f"docs/deploy.md 里没找到 §{section_no}"
    block = re.search(r"```ini\n(.*?)```", section.group(0), re.S)
    assert block, f"docs/deploy.md §{section_no} 里没找到 ```ini 代码块"
    return block.group(1)


def _norm(text: str) -> str:
    """去掉行尾空白和首尾空行——那些不影响 systemd，也不该让测试红。"""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _unit(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


# -- 结构性 ---------------------------------------------------------------


def test_every_unit_file_exists_and_has_the_three_sections() -> None:
    for name in UNITS:
        path = ROOT / "deploy" / name
        assert path.exists(), f"deploy/{name} 不见了"
        body = path.read_text(encoding="utf-8")
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in body, f"{name} 里少了 {section}"


# -- 一致性 ---------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(UNITS))
def test_doc_unit_matches_the_file(name: str) -> None:
    """核心断言：文档里那份和仓库里那份**逐行一致**。"""
    real = _norm(_unit(name))
    doc = _norm(_unit_block_in_doc(UNITS[name]))
    if doc != real:
        diff = "\n".join(
            difflib.unified_diff(
                real.splitlines(),
                doc.splitlines(),
                fromfile=f"deploy/{name}",
                tofile=f"docs/deploy.md §{UNITS[name]}",
                lineterm="",
            )
        )
        raise AssertionError(
            f"docs/deploy.md §{UNITS[name]} 的 systemd 单元和 deploy/{name} 不一致。\n"
            "照文档敲出来的单元会和正在跑的不是同一个文件——这正是当初漂过的地方。\n\n"
            + diff
        )


# -- 关键指令（挡住「两边一起改错」）-------------------------------------


def test_agent_unit_pins_the_things_that_actually_matter() -> None:
    """把关键指令单独钉一遍。

    纯一致性断言有个漏洞：**两边一起改错也还是一致**。
    实测验证过：把 SuccessExitStatus=143 从两边同时删掉，一致性断言是绿的。
    """
    body = _unit("yuque-agent.service")
    must_have = {
        # 时区靠程序自己钉死，但 HOME 得对，否则 uv / 凭证找不到
        "Environment=HOME=/home/yuque": "服务账号的家目录（凭证在它下面）",
        "EnvironmentFile=/home/yuque/.yuque/agent.env": "LLM key 从这儿进来",
        # 不加这个，每次正常停止都会在日志里留一串假 Failed（真故障被淹掉）
        "SuccessExitStatus=143": "SIGTERM 是正常停止，不是故障",
        # 加固：这个进程不需要新特权、也不该写自己的代码
        "NoNewPrivileges=true": "加固",
        "ProtectSystem=full": "加固",
        "User=yuque": "用专用用户跑，不用 root",
    }
    missing = [f"{k}（{why}）" for k, why in must_have.items() if k not in body]
    assert not missing, "单元里少了关键指令：\n  " + "\n  ".join(missing)


def test_plan_service_also_runs_unprivileged_and_has_no_baked_key() -> None:
    """下载口是整个项目唯一对外的口，这几件不能松。"""
    body = _unit("yuque-agent-plan.service")
    assert "User=yuque" in body
    # 密钥必须走 EnvironmentFile（600 的那个），不能写进单元
    # —— 单元是 644，世界可读。
    assert "EnvironmentFile=/home/yuque/.yuque/agent.env" in body
    assert "YQA_PLAN_KEY" not in body, "密钥不能写进单元文件（单元 644，公开可读）"
    assert "SuccessExitStatus=143" in body
    assert "ProtectSystem=full" in body
    # 端口与工作区必须显式写出来：默认值是给本机开发用的相对路径
    assert "/var/lib/yuque-agent/workspace" in body


def test_unit_never_runs_as_root() -> None:
    for name in UNITS:
        assert "User=root" not in _unit(name), f"{name} 不该用 root 跑"


def test_plan_service_is_not_in_the_agent_unit() -> None:
    """下载口和轮询是**两个**进程。

    合在一起的话，下载口崩一次就会把轮询带走（反之亦然），
    而这两个东西的可用性要求完全不同。
    """
    assert "serve-plan" not in _unit("yuque-agent.service")
    assert "serve-plan" in _unit("yuque-agent-plan.service")
