"""申请周期（cycle）的日期计算。

**目录命名约定**：``MMDD-MMDD``，覆盖 **周六 00:00 ~ 下周五 23:59**。

为什么不是「周一 ~ 周日」：教室必须**提前 2 天、最多 9 天**申请，所以每个周期的申请
——包括周日活动的申请——最晚在周五就提了。把周期的起点定在**周六 00:00**，
就让「归档时刻」与「周期翻转时刻」**完全重合**，不再存在任何错位。

┌─ 2026 年 9 月 ────────────────────────────────────────────────┐
│  周六      周日      周一      周二      周三      周四      周五  │
│  9/12     9/13     9/14     9/15     9/16     9/17     9/18      │  ← 周期 0912-0918
│  9/19     9/20     9/21     9/22     9/23     9/24     9/25      │  ← 周期 0919-0925
│  9/26     9/27     9/28     9/29     9/30     10/1     10/2      │  ← 周期 0926-1002
└────────────────────────────────────────────────────────────────┘

所以「今天」（9/20 周日）所在的周期是 ``0919-0925``，社员把申请写进这个目录。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime

DEFAULT_START_WEEKDAY = 5
"""周期起始日：0=周一 … 5=周六 … 6=周日。"""

DEFAULT_START_HOUR = 0
"""周期翻转时刻（小时）。周六 00:00。"""

_TITLE_RE = re.compile(r"^(\d{2})(\d{2})-(\d{2})(\d{2})$")


@dataclass(frozen=True)
class Week:
    """一个申请周期（起止都是闭区间，含首尾两天）。"""

    start: date
    """周期首日（周六）。"""
    end: date
    """周期末日（周五）。"""

    @property
    def title(self) -> str:
        return f"{self.start:%m%d}-{self.end:%m%d}"

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def __str__(self) -> str:
        return self.title


def cycle_of(day: date, *, start_weekday: int = DEFAULT_START_WEEKDAY) -> Week:
    """``day`` 落在哪个周期里。"""
    days_since = (day.weekday() - start_weekday) % 7
    start = day - timedelta(days=days_since)
    return Week(start=start, end=start + timedelta(days=6))


def cycle_boundary(
    now: datetime,
    *,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    start_hour: int = DEFAULT_START_HOUR,
) -> datetime:
    """最近一次（含今天）「周期翻转时刻」。同一周期内稳定不变。

    按 **``now`` 自己的时区** 算：把周期的起点日期贴上 ``now.tzinfo``。

    > 坑（实测踩到过）：不能写成
    > ``datetime.combine(day, dtime(hour=start_hour)).astimezone(now.tzinfo)``。
    > ``datetime.combine()`` 产出的是 **naive** datetime，而 naive 的 ``.astimezone(tz)``
    > 会**按系统本地时区**解释它——只有 ``now.tzinfo`` 恰好等于系统本地时区时才碰巧对。
    > 生产里 ``now`` 来自 ``datetime.now().astimezone()``，正好满足这个条件，
    > 所以 live 跑和测试都看不出来；一旦调用方传进别的时区（库调用、测试注入、
    > 或将来显式指定时区）就会错到**隔壁周期**去（实测：传 UTC 会得到
    > ``0905-0911`` 而不是 ``0912-0918``）。
    """
    days_since = (now.weekday() - start_weekday) % 7
    day = now.date() - timedelta(days=days_since)
    return datetime.combine(day, dtime(hour=start_hour), tzinfo=now.tzinfo)


def cycle_targets(
    now: datetime,
    *,
    start_weekday: int = DEFAULT_START_WEEKDAY,
    start_hour: int = DEFAULT_START_HOUR,
) -> tuple[Week, Week]:
    """返回 ``(当前周期, 上一个周期)``。

    这是**纯粹的算术**，所以归程序算，不让 LLM 猜。

    ┌─ 例：周期起始 = 周六 00:00 ────────────────────────────────┐
    │ 周六 09-19 00:30 跑 →  当前 0919-0925 / 上一个 0912-0918    │
    │ 周日 09-20 10:00 跑 →  当前 0919-0925 / 上一个 0912-0918    │
    │ 周五 09-25 23:00 跑 →  当前 0919-0925 / 上一个 0912-0918    │
    │ 周六 09-26 00:30 跑 →  当前 0926-1002 / 上一个 0919-0925    │
    └────────────────────────────────────────────────────────────┘
    """
    boundary = cycle_boundary(now, start_weekday=start_weekday, start_hour=start_hour)
    current = cycle_of(boundary.date(), start_weekday=start_weekday)
    previous = cycle_of(boundary.date() - timedelta(days=7), start_weekday=start_weekday)
    return current, previous


def is_cycle_title(title: str) -> bool:
    return bool(_TITLE_RE.match((title or "").strip()))


def parse_cycle_title(title: str, *, today: date | None = None) -> Week | None:
    """把 ``0919-0925`` 解析成具体日期。跨年时按「离今天最近的那个」猜年份。"""
    match = _TITLE_RE.match((title or "").strip())
    if not match:
        return None
    anchor = today or date.today()
    month, day = int(match.group(1)), int(match.group(2))
    end_month, end_day = int(match.group(3)), int(match.group(4))
    wraps = (end_month, end_day) < (month, day)
    try:
        start = date(anchor.year, month, day)
    except ValueError:
        return None
    if abs((start - anchor).days) > 200:
        shift = 1 if start < anchor else -1
        try:
            start = date(anchor.year + shift, month, day)
        except ValueError:
            return None
    try:
        end = date(start.year + (1 if wraps else 0), end_month, end_day)
    except ValueError:
        return None
    return Week(start=start, end=end)
