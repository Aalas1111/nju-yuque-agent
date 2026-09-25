"""全局测试隔离。

## 为什么要重定向 `$HOME`（拿生产事故换来的）

2026-09-23 有人在**生产服务器**上跑 `pytest`，测试真的执行了一次凭证登出，
把 `/home/yuque/.yuque/qqbot.json`（扫码换来的凭证）连同备份一起删了——
删一次就要重扫一次码，当天发生了两次。

根因是「测试假设默认路径下没有东西」：开发机上确实没有，服务器上正好有。
所以这里把 `$HOME` 重定向到临时目录，让**任何**走默认路径的代码都落进沙箱。
今天它还守着语雀 token（`~/.yuque/auth.json`）与 `~/.yuque/agent.env`。

这不是「把测试写得更小心」——那要靠人记得；这是让整个测试套件**够不着**真实家目录。
（事故里的 QQ 桥已拆成独立项目，它自己的凭证由它自己的测试守。）
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """把 `$HOME` 指到临时目录。

    * POSIX：`Path.home()` 看 `HOME`；
    * Windows：看 `USERPROFILE`（其次 `HOMEDRIVE` + `HOMEPATH`）——
      开发在 Windows、跑在 Linux，两边都要盖住。
    """
    home = tmp_path_factory.mktemp("fake-home")
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    if os.name == "nt":  # pragma: no cover - 只在 Windows 开发机上走到
        drive, tail = os.path.splitdrive(str(home))
        os.environ["HOMEDRIVE"] = drive or "C:"
        os.environ["HOMEPATH"] = tail or "\\"
    return home
