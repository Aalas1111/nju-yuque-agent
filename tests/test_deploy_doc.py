"""守卫：`docs/deploy.md` 里的 systemd 单元必须和 `deploy/yuque-agent.service` 一致。

为什么要有这条：这个单元**曾经漂过**——文档里少了 `Documentation=` 和
`SyslogIdentifier=` 两个真机上真有的指令，`ExecStart` 还是折成三行的写法。
照文档敲出来的单元和正在跑的不是同一个文件，而这种事**没人会去对账**。

现在仓库里有一份权威副本（`deploy/yuque-agent.service`，直接从真机取的），
文档里那份是给人读的。两边只要不一致，这个测试就失败。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_FILE = ROOT / "deploy" / "yuque-agent.service"
DEPLOY_DOC = ROOT / "docs" / "deploy.md"


def _unit_block_in_doc() -> str:
    """把 `docs/deploy.md` §4 里那个 ```ini 代码块抠出来。"""
    text = DEPLOY_DOC.read_text(encoding="utf-8")
    section = re.search(r"^## 4\..*?(?=^## 5\.)", text, re.S | re.M)
    assert section, "docs/deploy.md 里没找到 §4"
    block = re.search(r"```ini\n(.*?)```", section.group(0), re.S)
    assert block, "docs/deploy.md §4 里没找到 ```ini 代码块"
    return block.group(1)


def _norm(text: str) -> str:
    """去掉行尾空白和首尾空行——那些不影响 systemd，也不该让测试红。"""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def test_unit_file_exists_and_has_the_three_sections() -> None:
    assert UNIT_FILE.exists(), "deploy/yuque-agent.service 不见了"
    body = UNIT_FILE.read_text(encoding="utf-8")
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in body, f"单元里少了 {section}"


def test_doc_unit_matches_the_file() -> None:
    """核心断言：文档里那份和仓库里那份**逐行一致**。"""
    doc = _norm(_unit_block_in_doc())
    real = _norm(UNIT_FILE.read_text(encoding="utf-8"))
    if doc != real:
        import difflib

        diff = "\n".join(
            difflib.unified_diff(
                real.splitlines(),
                doc.splitlines(),
                fromfile="deploy/yuque-agent.service",
                tofile="docs/deploy.md §4",
                lineterm="",
            )
        )
        raise AssertionError(
            "docs/deploy.md §4 的 systemd 单元和 deploy/yuque-agent.service 不一致。\n"
            "照文档敲出来的单元会和正在跑的不是同一个文件——这正是当初漂过的地方。\n\n" + diff
        )


def test_unit_pins_the_things_that_actually_matter() -> None:
    """把关键指令单独钉一遍。

    纯「两边一致」的断言有个漏洞：**一起改错也还是一致**。
    所以对几件真出过事 / 一出事就难查的事再单独断言。
    """
    body = UNIT_FILE.read_text(encoding="utf-8")
    must_have = {
        # 时区靠程序自己钉死，但 HOME 得对，否则 uv / 凭证找不到
        "Environment=HOME=/home/yuque": "服务账号的家目录（凭证在它下面）",
        "EnvironmentFile=/home/yuque/.yuque/agent.env": "LLM key 从这儿进来",
        # 不加这个，每次正常停止都会在日志里留一串假 Failed（真故障被淹掉）
        "SuccessExitStatus=143": "SIGTERM 是正常停止，不是故障",
        # 加固：这个进程不需要新特权
        "NoNewPrivileges=true": "加固",
        "ProtectSystem=full": "加固",
        # 别让服务去写自己的代码
        "User=yuque": "用专用用户跑，不用 root",
    }
    missing = [f"{k}（{why}）" for k, why in must_have.items() if k not in body]
    assert not missing, "单元里少了关键指令：\n  " + "\n  ".join(missing)


def test_unit_never_runs_as_root() -> None:
    assert "User=root" not in UNIT_FILE.read_text(encoding="utf-8")
