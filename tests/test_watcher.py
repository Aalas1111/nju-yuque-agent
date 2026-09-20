"""常驻轮询的触发逻辑。

要锁住的：

1. 没有变化时**不唤醒 LLM**（这是「静默」这个性质的唯一实现点）；
2. 归档是**时钟驱动**的——即使知识库一个字都没变也要跑；
3. 归档在同一个周期内**只跑一次**（进程重启/跨天补跑都不能重复归档）；
4. 归档**带补跑**：进程错过了翻转时刻，启动后要补上；
5. 一轮异常不能把常驻进程搞死。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pytest

from yuque_agent import clock
from yuque_agent.agent import RunResult
from yuque_agent.config import Settings
from yuque_agent.runner import State
from yuque_agent.watcher import Watcher


def dt(text: str) -> datetime:
    """测试里的「现在」**必须用生产时区**（Asia/Shanghai）构造。

    不能写成 ``datetime.fromisoformat(text).astimezone()``——那是系统本地时区，
    跑在 UTC 机器上的 CI 里注入的时刻就和生产路径不是同一个时区了，
    测出来的东西和线上不是一回事。
    """
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=clock.TZ) if parsed.tzinfo is None else parsed.astimezone(clock.TZ)


@dataclass
class FakeRunner:
    state: State = field(default_factory=State)
    polls: int = 0
    archives: int = 0
    last_poll_kwargs: dict = field(default_factory=dict)
    poll_result: object = None

    def poll_once(self, *, force: bool = False, rescan: bool = False, now=None, debounce=True):
        self.polls += 1
        self.last_poll_kwargs = {"now": now, "debounce": debounce, "rescan": rescan}
        return self.poll_result

    def archive_once(self, *, now: datetime | None = None):
        self.archives += 1
        # 模仿真 Runner：把水位线推进到「当前周期」
        from yuque_agent.week import cycle_targets

        moment = now or clock.now()
        current, _ = cycle_targets(moment)
        self.state.last_archive_title = current.title
        return RunResult(run_id=f"a{self.archives}", kind="archive", verdict="nothing_to_do")


def runner_at(cycle_title: str = "", **kwargs) -> FakeRunner:
    """造一个「本周期归档已经做过」的 runner（水位线 = 周期目录名）。"""
    return FakeRunner(state=State(last_archive_title=cycle_title), **kwargs)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


def watcher(settings: Settings, runner: FakeRunner, logs: list[str] | None = None) -> Watcher:
    sink = logs.append if logs is not None else (lambda *_: None)
    return Watcher(runner=runner, settings=settings, log=sink)  # type: ignore[arg-type]


CURRENT = "0919-0925"


# ---------------------------------------------------------------- 轮询的静默性


def test_nothing_changed_is_silent(settings: Settings) -> None:
    runner = runner_at(CURRENT, poll_result=None)
    lines = watcher(settings, runner).tick(dt("2026-09-20T10:00"))
    assert runner.polls == 1
    assert runner.archives == 0
    assert lines == [], "没有变化就不该有任何输出，更不该唤醒 LLM"


def test_changed_round_is_reported(settings: Settings) -> None:
    runner = runner_at(
        CURRENT,
        poll_result=RunResult(run_id="r1", kind="polling", verdict="accepted", summary="受理了"),
    )
    lines = watcher(settings, runner).tick(dt("2026-09-20T10:00"))
    assert len(lines) == 1
    assert "受理了" in lines[0]


# ---------------------------------------------------------------- 归档时机


@pytest.mark.parametrize(
    ("moment", "watermark", "should_fire"),
    [
        # 水位线=本周期 → 已经归档过了，不再触发
        ("2026-09-19T00:00", CURRENT, False),
        ("2026-09-22T12:00", CURRENT, False),
        ("2026-09-25T23:59", CURRENT, False),
        # 跨到下一个周期 → 该归档了
        ("2026-09-26T00:00", CURRENT, True),
        ("2026-09-26T00:01", CURRENT, True),
        ("2026-09-28T09:00", CURRENT, True),
        # 水位线是上上个周期（进程错过了整个周期）→ 补跑
        ("2026-09-20T10:00", "0912-0918", True),
        # 全新进程（没有水位线）→ 立刻补跑
        ("2026-09-20T10:00", "", True),
    ],
)
def test_archive_due(settings: Settings, moment: str, watermark: str, should_fire: bool) -> None:
    runner = runner_at(watermark)
    w = watcher(settings, runner)
    now = dt(moment)
    assert w.archive_due(now) is should_fire
    w.tick(now)
    assert runner.archives == (1 if should_fire else 0)
    # 归档和轮询在同一 tick 里互斥
    assert runner.polls == (0 if should_fire else 1)


def test_archive_runs_once_per_cycle(settings: Settings) -> None:
    """同一个周期内反复 tick（含跨天）都不该重复归档。"""
    runner = FakeRunner()  # 空水位线 → 第一次 tick 就补跑归档
    w = watcher(settings, runner)

    w.tick(dt("2026-09-19T00:01"))
    assert runner.archives == 1
    assert runner.state.last_archive_title == CURRENT

    w.tick(dt("2026-09-20T10:00"))
    w.tick(dt("2026-09-25T23:59"))
    assert runner.archives == 1, "同一周期内不该重复归档"
    assert runner.polls == 2, "归档做过之后应当回到普通轮询"


def test_archive_fires_again_next_cycle(settings: Settings) -> None:
    runner = FakeRunner()
    w = watcher(settings, runner)
    w.tick(dt("2026-09-19T00:01"))
    w.tick(dt("2026-09-26T00:01"))
    assert runner.archives == 2
    assert runner.state.last_archive_title == "0926-1002"


def test_archive_boundary_is_exactly_the_cycle_flip(settings: Settings) -> None:
    """归档时刻必须与周期翻转时刻重合——这是这次重构的全部意义。"""
    w = watcher(settings, FakeRunner())
    boundary = w.scheduled_archive_at(dt("2026-09-20T10:00"))
    assert (boundary.month, boundary.day, boundary.hour, boundary.minute) == (9, 19, 0, 0)


def test_archive_can_be_disabled(settings: Settings) -> None:
    settings.archive_enabled = False
    runner = FakeRunner()  # 即使水位线为空也不该触发
    watcher(settings, runner).tick(dt("2026-09-19T00:01"))
    assert runner.archives == 0
    assert runner.polls == 1


# ---------------------------------------------------------------- 韧性


def test_run_forever_survives_a_failing_round(settings: Settings) -> None:
    """常驻进程不能因为一轮异常就死掉。"""

    class Boom(FakeRunner):
        def poll_once(self, *, force: bool = False, rescan: bool = False, now=None, debounce=True):
            raise RuntimeError("网络断了")

    runner = Boom(state=State(last_archive_title=CURRENT))
    logs: list[str] = []
    w = watcher(settings, runner, logs)
    w.run_forever(max_ticks=1, sleep=lambda _s: None)
    assert any("异常" in line for line in logs), logs
