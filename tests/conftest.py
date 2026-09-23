"""全局测试隔离。

## 为什么要重定向 `$HOME`（拿生产事故换来的）

有人在**生产服务器**上跑 `pytest`，测试里的 `yqa qq logout --yes` 把
`/home/yuque/.yuque/qqbot.json` **连同备份一起删了** —— 凭证是扫码换来的，
删了就得重新扫一次码。

根因有两个，都得修：

1. `tests/test_qq_cli.py` 里那两个测试的前提是「默认路径下没有凭证」。
   在开发机上确实没有，在服务器上正好有 —— 于是 `logout` 真的执行了，
   `notify` 也真的去找了凭证。
2. `credentials.DEFAULT_CREDENTIALS_PATH` 是**模块级常量**，`Path.home()`
   在导入时就求值了 —— 所以光在测试里改 `HOME` 拦不住它。
   （已改成惰性求值的 `default_credentials_path()`。）

所以这里把 `$HOME` 重定向到临时目录，让**任何**走默认路径的代码都落在沙箱里。
这不是「把测试写得更小心」——那要靠人记得；这是让整个测试套件**够不着**真实家目录。
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
