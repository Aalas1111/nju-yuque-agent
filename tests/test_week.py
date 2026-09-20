"""日期周期：目录命名与归档目标。

周期 = **周六 00:00 ~ 下周五 23:59**，目录名 = `MMDD-MMDD`。

这一组测试锁住的是「程序负责算术、LLM 负责判断」这条分工线里，**属于程序的那一半**。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from yuque_agent.week import (
    cycle_boundary,
    cycle_of,
    cycle_targets,
    is_cycle_title,
    parse_cycle_title,
)


def dt(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone()


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
