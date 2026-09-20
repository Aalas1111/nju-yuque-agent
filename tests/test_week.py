"""日期周期：目录命名与归档目标。

周期 = **周六 00:00 ~ 下周五 23:59**，目录名 = `MMDD-MMDD`。

这一组测试锁住的是「程序负责算术、LLM 负责判断」这条分工线里，**属于程序的那一半**。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from yuque_agent import clock
from yuque_agent.week import (
    cycle_boundary,
    cycle_of,
    cycle_targets,
    is_cycle_title,
    parse_cycle_title,
)


def dt(text: str) -> datetime:
    """测试里的「现在」**必须用生产时区**（Asia/Shanghai）构造。

    不能写成 ``datetime.fromisoformat(text).astimezone()``——那是系统本地时区，
    跑在 UTC 机器上的 CI 里注入的时刻就和生产路径不是同一个时区了，
    测出来的东西和线上不是一回事。
    """
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=clock.TZ) if parsed.tzinfo is None else parsed.astimezone(clock.TZ)


def test_cycle_starts_on_saturday() -> None:
    cycle = cycle_of(date(2026, 9, 20))  # 周日
    assert cycle.start == date(2026, 9, 19)
    assert cycle.end == date(2026, 9, 25)
    assert cycle.title == "0919-0925"


def test_every_day_of_the_cycle_maps_to_the_same_cycle() -> None:
    """周六 ~ 周五 七天必须落在同一个周期里。"""
    titles = {cycle_of(date(2026, 9, 19 + i)).title for i in range(7)}
    assert titles == {"0919-0925"}


def test_next_saturday_starts_a_new_cycle() -> None:
    assert cycle_of(date(2026, 9, 25)).title == "0919-0925"  # 周五，仍是本周期
    assert cycle_of(date(2026, 9, 26)).title == "0926-1002"  # 周六，新周期


def test_cycle_contains() -> None:
    cycle = cycle_of(date(2026, 9, 20))
    assert cycle.contains(date(2026, 9, 19))
    assert cycle.contains(date(2026, 9, 25))
    assert not cycle.contains(date(2026, 9, 18))
    assert not cycle.contains(date(2026, 9, 26))


@pytest.mark.parametrize(
    ("moment", "current", "previous"),
    [
        # 周期翻转点 = 周六 00:00
        ("2026-09-18T23:59", "0912-0918", "0905-0911"),
        ("2026-09-19T00:01", "0919-0925", "0912-0918"),
        ("2026-09-20T10:00", "0919-0925", "0912-0918"),
        ("2026-09-25T23:59", "0919-0925", "0912-0918"),
        ("2026-09-26T00:01", "0926-1002", "0919-0925"),
        ("2026-10-03T09:00", "1003-1009", "0926-1002"),
    ],
)
def test_cycle_targets(moment: str, current: str, previous: str) -> None:
    a, b = cycle_targets(dt(moment))
    assert (a.title, b.title) == (current, previous)


def test_cycle_targets_are_stable_within_one_cycle() -> None:
    """同一个周期内反复算，结果必须一样——否则常驻进程重启就会重复归档。"""
    assert cycle_targets(dt("2026-09-19T00:01")) == cycle_targets(dt("2026-09-22T12:00"))
    assert cycle_targets(dt("2026-09-22T12:00")) == cycle_targets(dt("2026-09-25T23:59"))


def test_cycle_boundary_is_the_most_recent_saturday_midnight() -> None:
    boundary = cycle_boundary(dt("2026-09-20T10:00"))
    assert (boundary.year, boundary.month, boundary.day, boundary.hour) == (2026, 9, 19, 0)


def test_boundary_and_targets_agree() -> None:
    """归档时刻与周期翻转时刻必须**完全重合**——这是这次重构的全部意义。"""
    for moment in ("2026-09-19T00:01", "2026-09-22T12:00", "2026-09-26T00:01"):
        now = dt(moment)
        boundary = cycle_boundary(now)
        current, _ = cycle_targets(now)
        assert current.start == boundary.date()
        assert current.title == cycle_of(boundary.date()).title


def test_cycle_title_parsing() -> None:
    assert is_cycle_title("0919-0925")
    assert not is_cycle_title("归档区")
    assert not is_cycle_title("0919")
    parsed = parse_cycle_title("0919-0925", today=date(2026, 9, 20))
    assert parsed is not None
    assert parsed.start == date(2026, 9, 19)
    assert parsed.end == date(2026, 9, 25)


def test_cycle_title_parsing_across_year_end() -> None:
    parsed = parse_cycle_title("1226-0101", today=date(2026, 12, 28))
    assert parsed is not None
    assert parsed.start == date(2026, 12, 26)
    assert parsed.end == date(2027, 1, 1)


def test_older_cycles_sort_before_newer() -> None:
    """归档区内部按时间「最新在上」要靠它排序。"""
    today = date(2026, 9, 20)
    older = parse_cycle_title("0912-0918", today=today)
    newer = parse_cycle_title("0919-0925", today=today)
    assert older and newer
    assert older.start < newer.start


# ------------------------------------------------- 时区（部署到别人的服务器时会踩）


def test_cycle_always_contains_nows_own_date_in_any_timezone() -> None:
    """`cycle_targets` / `cycle_boundary` 必须按 **now 自己的时区** 算。

    回归：以前 `cycle_boundary` 写成
    `datetime.combine(day, dtime(hour)).astimezone(now.tzinfo)`，
    而 `combine()` 产出的是 **naive** datetime——naive 的 `.astimezone(tz)` 会按
    **系统本地时区**解释它。生产里 `now = datetime.now().astimezone()`，tzinfo 恰好
    就是系统本地时区，所以一直碰巧正确，测试也全用系统时区（`dt()` 走 `.astimezone()`），
    于是这个 bug 完全没被盖住。

    这条断言不依赖任何具体时区：**算出来的周期必须包含 now 自己的日期**。
    """
    zones = [
        timezone(timedelta(hours=8), "CST"),  # 北京（生产目标）
        UTC,  # 云服务器默认，很容易踩
        timezone(timedelta(hours=-5), "EST"),  # 再来一个反方向的
    ]
    moments = [
        "2026-09-19T00:01",
        "2026-09-20T10:00",
        "2026-09-22T12:00",
        "2026-09-25T23:59",
        "2026-09-26T00:01",
    ]
    for tz in zones:
        for text in moments:
            now = datetime.fromisoformat(text).replace(tzinfo=tz)
            current, previous = cycle_targets(now)
            assert current.contains(now.date()), (
                f"时区 {tz} 下 {now} 算出的当前周期 {current.title} 不含它自己的日期"
            )
            assert previous.contains(now.date() - timedelta(days=7))
            assert current.start == cycle_boundary(now).date()
            assert current.end - current.start == timedelta(days=6)


def test_cycle_boundary_keeps_the_timezone_of_now() -> None:
    """边界时刻必须带 `now` 的时区，而不是被 system 本地时区带跑。"""
    utc_tz = UTC
    boundary = cycle_boundary(datetime(2026, 9, 20, 10, 0, tzinfo=utc_tz))
    assert boundary.tzinfo is not None
    assert boundary.utcoffset() == timedelta(0), f"应当仍是 UTC，实际 {boundary}"
    assert (boundary.month, boundary.day, boundary.hour) == (9, 19, 0)
