"""常驻轮询：一个进程里跑两个互不干扰的触发器。

| 触发器 | 类型 | 行为 |
|---|---|---|
| 轮询 | 事件驱动 | 每 ``interval`` 秒看一眼知识库；**没变化就什么都不做** |
| 归档 | 时钟驱动 | 每周六 ``archive_hour`` 点（默认 08:00）唤醒一次，**与「有没有变化」无关** |

两者共用同一个 agent，但**注册的工具集不同**（见 :mod:`.tools`）——
这就是本项目「用能力边界兜底误操作」的落点。

归档带**补跑**能力：进程周六没在跑，周日或周一启动时会补上（以「本周的周六 08:00 是否已过」
且「本轮归档还没做过」为判据）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from . import clock, control
from .agent import RunResult
from .config import Settings
from .runner import Runner
from .week import cycle_boundary, cycle_targets

LogFn = Callable[[str], None]


@dataclass
class Watcher:
    runner: Runner
    settings: Settings
    log: LogFn = print

    last_result: RunResult | None = None
    """最近一次真的跑了 LLM 的结果（``None`` = 没变化/在静默期）。QQ 状态查询用它。"""

    last_kind: str = ""
    """最近一次结果的类型：``polling`` / ``archive``。"""

    # -- 归档时机 ---------------------------------------------------------
    def scheduled_archive_at(self, now: datetime) -> datetime:
        """最近一次（含今天）周期翻转时刻。同一周期内保持不变。"""
        return cycle_boundary(
            now,
            start_weekday=self.settings.archive_weekday,
            start_hour=self.settings.archive_hour,
        )

    def archive_due(self, now: datetime) -> bool:
        """到点了、且本周期还没做过。

        "本周期" 用「当前周期的目录名」标识——它在一整个周期内不变，
        所以进程重启、跨天补跑都不会重复归档。
        """
        if not self.settings.archive_enabled:
            return False
        if now < self.scheduled_archive_at(now):
            return False
        current, _ = cycle_targets(
            now,
            start_weekday=self.settings.archive_weekday,
            start_hour=self.settings.archive_hour,
        )
        return self.runner.state.last_archive_title != current.title

    # -- 单步 -------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or clock.now()
        lines: list[str] = []

        if self.archive_due(now):
            self.log(f"[{now:%H:%M:%S}] [archive] 到周期翻转时刻，唤醒归档会话")
            result = self.runner.archive_once(now=now)
            self.last_result = result
            self.last_kind = "archive"
            lines.append(_summarize("archive", result))
            for line in lines:
                self.log(line)
            return lines

        result = self.runner.poll_once(now=now, debounce=self.settings.quiet_seconds > 0)
        if result is None:
            return lines
        self.last_result = result
        self.last_kind = "polling"
        lines.append(_summarize("polling", result))
        for line in lines:
            self.log(line)
        return lines

    # -- 常驻 -------------------------------------------------------------
    def run_forever(
        self,
        *,
        max_ticks: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
        after_tick: Callable[[], None] | None = None,
    ) -> None:
        """常驻循环。

        ``after_tick`` 每轮结束后调用一次（用于把 ``outbox/notify`` 投出去这类
        「跟本轮结果无关」的杂活）。它抛异常不会终止常驻进程。
        """
        ticks = 0
        self.log(
            f"[watch] 开始常驻：每 {self.settings.interval}s 轮询 {self.settings.repo}；"
            f"发现变化后等 {self.settings.quiet_seconds}s 静默期再唤醒 LLM；"
            f"周期翻转/归档时刻 = 每周{'一二三四五六日'[self.settings.archive_weekday]} "
            f"{self.settings.archive_hour:02d}:00"
            + ("（dry-run）" if self.settings.dry_run else "")
            + ("（写工作日志）" if self.settings.journal else "")
        )
        while True:
            # 人工请求优先于定时轮询：QQ 桥（或别的下游）写进 control/requests/
            # 的 /run、/archive、/apply 在这里被消费——**只有这一个进程碰 state.json**。
            try:
                control.process_pending(self.settings, runner=self.runner, log=self.log)
            except Exception as exc:  # noqa: BLE001 - 队列出问题不能拖垮常驻
                self.log(f"[watch] 控制请求处理异常（已忽略并继续）：{type(exc).__name__}: {exc}")
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - 常驻进程不能因为一次失败就死
                self.log(f"[watch] 本轮异常（已忽略并继续）：{type(exc).__name__}: {exc}")
            if after_tick is not None:
                try:
                    after_tick()
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[watch] 收尾任务异常（已忽略并继续）：{type(exc).__name__}: {exc}")
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                self.log(f"已达 max_ticks={max_ticks}，退出")
                return
            sleep(self.settings.interval)


def _summarize(kind: str, result: RunResult) -> str:
    usage = result.usage
    bits = [
        f"kind={kind}",
        f"verdict={result.verdict or '—'}",
        f"steps={result.steps}",
        f"tools={result.tool_calls}",
        f"tokens={usage.prompt_tokens}/{usage.completion_tokens}",
    ]
    if result.emitted:
        kinds = ", ".join(str(e.get("type")) for e in result.emitted)
        bits.append(f"产出=[{kinds}]")
    if result.kb_writes:
        bits.append(f"语雀写操作={len(result.kb_writes)}")
    if result.error:
        bits.append(f"ERROR={result.error}")
    head = f"{result.summary}" if result.summary else "(无摘要)"
    return f"{head}\n   " + " · ".join(bits)
