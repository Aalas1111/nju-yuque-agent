"""session 记录：一次 run 的**唯一事实来源**。

设计取舍：

* **JSONL，一行一事件**——追加写、天然可流式、坏了一行不影响其余；
* **每一行都立刻 flush**——进程被杀也留得下已经发生的部分（agent 崩溃时的现场最值钱）；
* **存原始 reasoning_content**——「它当时为什么这么判」是调试工作流的核心证据；
* 事件类型刻意少（``run_start / system / user / assistant / tool / error / run_end``），
  渲染成人话是 :mod:`.render` 的事，记录层不参与展示。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import clock


def now_iso() -> str:
    return clock.stamp()


@dataclass
class SessionRecorder:
    path: Path
    _fh: Any = field(default=None, repr=False)
    events_written: int = 0

    def open(self) -> SessionRecorder:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")
        return self

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> SessionRecorder:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 写 ---------------------------------------------------------------
    def event(self, event_type: str, **fields: Any) -> None:
        record = {"t": event_type, "ts": now_iso(), **fields}
        line = json.dumps(record, ensure_ascii=False)
        if self._fh is not None:
            self._fh.write(line + "\n")
            self._fh.flush()
        self.events_written += 1

    # -- 读 ---------------------------------------------------------------
    def read(self) -> list[dict[str, Any]]:
        return list(read_events(self.path))


def read_events(path: Path) -> Iterator[dict[str, Any]]:
    """逐行读 session；损坏的行跳过而不是整份作废。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record


@dataclass
class Stopwatch:
    """给每次工具调用计时。"""

    _start: float = field(default_factory=time.perf_counter)

    def ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)
