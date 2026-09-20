"""提示词加载。

提示词是**纯文本文件**（``prompts/*.md``），不是代码里的字符串常量——
因为它们是这个项目里**最需要反复迭代**的东西，应该能改完立刻跑、也能被 diff 与评审。
"""

from __future__ import annotations

from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"

PROMPT_FILES = {
    "polling": "polling.md",
    "archive": "archive.md",
}


class PromptError(RuntimeError):
    pass


class PromptLoader:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else PROMPT_DIR

    def path_for(self, kind: str) -> Path:
        name = PROMPT_FILES.get(kind)
        if name is None:
            raise PromptError(f"未知的 run 类型 {kind!r}，可选：{sorted(PROMPT_FILES)}")
        return self.directory / name

    def load(self, kind: str) -> str:
        path = self.path_for(kind)
        if not path.is_file():
            raise PromptError(f"提示词文件不存在：{path}")
        return path.read_text(encoding="utf-8").strip()
