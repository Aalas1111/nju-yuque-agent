"""守卫：测试套件够不着真实家目录。

**这是拿生产事故换来的一组断言。** 有人在生产服务器上跑 `pytest`，
`tests/test_qq_cli.py::test_logout_without_credentials` 里的 `yqa qq logout --yes`
真的执行了，把 `/home/yuque/.yuque/qqbot.json` 连同备份一起删掉——
那是扫码换来的凭证，删了就要重新扫。

两个前提都得钉住，少一个洞就还在：

1. `$HOME` 在测试期间是**沙箱**（tests/conftest.py 那个 session 级 fixture）；
2. 默认凭证路径**惰性求值**（否则 `Path.home()` 在导入时就固化了，
   重定向 `HOME` 根本拦不住它）—— 这条当初就是漏的。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

from yuque_agent.qqbot import credentials
from yuque_agent.qqbot.config import default_config_path
from yuque_agent.qqbot.credentials import credentials_path


def test_home_is_a_sandbox(isolated_home: Path) -> None:
    """整套测试跑在临时家目录里，而不是开发者/服务器的真实家目录。"""
    assert Path.home() == isolated_home
    assert os.environ["HOME"] == str(isolated_home)
    # 沙箱里不该有任何真东西
    assert not (isolated_home / ".yuque" / "qqbot.json").exists()


def test_default_credentials_path_is_inside_the_sandbox(isolated_home: Path) -> None:
    """默认凭证路径必须落在沙箱内 —— 这正是当初漏掉的那条。"""
    path = credentials_path()
    assert isolated_home in path.parents, (
        f"默认凭证路径跑到沙箱外了：{path}\n这意味着某个测试能碰到真实的 ~/.yuque/qqbot.json。"
    )
    assert path.name == "qqbot.json"


def test_default_credentials_path_is_lazy_and_follows_home(tmp_path: Path, monkeypatch) -> None:
    """惰性求值：改 `HOME` 之后立刻反映出来。

    （当初是模块级常量 `DEFAULT_CREDENTIALS_PATH = Path.home() / ...`，
    导入时就固化了，所以测试里重定向 `HOME` 完全无效。）
    """
    assert "DEFAULT_CREDENTIALS_PATH" not in dir(credentials), (
        "又回到模块级常量了——那样 Path.home() 在导入时求值，测试重定向 HOME 会失效"
    )
    elsewhere = tmp_path / "another-home"
    monkeypatch.setenv("HOME", str(elsewhere))
    monkeypatch.setenv("USERPROFILE", str(elsewhere))
    assert credentials_path().parent.parent == elsewhere


def _real_home_literals(text: str) -> list[tuple[int, str]]:
    """找出「看起来就是一条家目录路径」的字符串字面量。

    只看**以 ``~`` 或 ``/home/`` 开头**的字面量——那才是「真去碰家目录」的写法。
    像 ``"Environment=HOME=/home/yuque"`` 那种「要搜索的子串」不是路径，不算。

    跳过文档字符串（它们只是说明文字，之前那条粗糙的文本扫描就是这么误报的）。
    """
    tree = ast.parse(text)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            value = node.value.strip()
            if value.startswith(("~", "/home/", "\\home\\")):
                found.append((node.lineno, value))
    return found


def test_no_test_file_hardcodes_the_real_home() -> None:
    """测试里不许写死家目录路径。

    挡的是「绕过 fixture 直接构造真实路径」这种写法——那是静态的，行为断言盖不住。
    但**不能误报**：一条会叫狼来的守卫比没有守卫更糟，它会训练人忽略它。
    所以这里：① 只看路径形状的字面量；② 跳过文档字符串；
    ③ 跳过本文件（它必须包含那些字面量才能描述规则）。
    """
    tests_dir = Path(__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        for line_no, value in _real_home_literals(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{line_no}  {value}")
    assert not offenders, "测试里写死了家目录路径：\n  " + "\n  ".join(offenders)


def test_credentials_store_in_a_subprocess_also_lands_in_the_sandbox(
    isolated_home: Path, tmp_path: Path
) -> None:
    """子进程也必须在沙箱里 —— `runner.invoke` 之外的路径同样不能漏。

    这条比断言常量更硬：真起一个 python 子进程问它「你的凭证文件在哪」，
    因为 `$HOME` 是**进程环境**，只有真的传下去才算数。
    """
    script = (
        "import sys; sys.path.insert(0, r'%s');"
        "from yuque_agent.qqbot.credentials import credentials_path;"
        "print(credentials_path())" % (Path(__file__).resolve().parent.parent / "src")
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert str(isolated_home) in out, f"子进程看到的是 {out}，不在沙箱里"


def test_config_default_path_does_not_use_home(tmp_path: Path) -> None:
    """工作区配置（notify/inbound 那张表）走工作区，不走家目录 —— 顺便钉住。"""

    class _S:
        root = tmp_path / "ws" / "g_kb"

    assert default_config_path(_S()) == _S.root / "qqbot.json"
