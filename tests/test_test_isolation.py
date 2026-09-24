"""守卫：测试套件够不着真实家目录。

**这是拿生产事故换来的一组断言。** 有人在生产服务器上跑 `pytest`，
某个测试里的 `yqa qq logout --yes` 真的执行了，把 `/home/yuque/.yuque/qqbot.json`
连同备份一起删掉——那是扫码换来的凭证，删了就要重新扫（事件与根因见 AGENTS.md §2.1）。

QQ 桥已拆到它自己的仓库（`docs/interface.md`），但那节课对**核心**一样成立：
核心的凭证（`~/.yuque/auth.json`、`agent.env`）也在家目录下。
所以「`$HOME` 在测试期间必须是沙箱」这条继续钉在这里。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

CORE_CREDENTIAL_FILES = ("auth.json", "agent.env")


def test_home_is_a_sandbox(isolated_home: Path) -> None:
    """整套测试跑在临时家目录里，而不是开发者/服务器的真实家目录。"""
    assert Path.home() == isolated_home
    assert os.environ["HOME"] == str(isolated_home)
    # 沙箱里不该有任何真东西（核心的两份凭证都不在）
    for name in CORE_CREDENTIAL_FILES:
        assert not (isolated_home / ".yuque" / name).exists(), f"沙箱里出现了 {name}"


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
