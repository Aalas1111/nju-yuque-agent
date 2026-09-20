"""学校侧词汇表：校区代码、教学楼代码、时间→节次。

这一层是「程序负责测量、LLM 负责判断」里的**测量**那一半。它必须**确定、可测**——
因为下游（教室借用插件）是纯程序，一个错的教学楼代码会让它去查错的教学楼。
"""

from __future__ import annotations

from datetime import date

import pytest

from yuque_agent import school

# ---------------------------------------------------------------- 校区


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("仙林", "3"),
        ("仙林校区", "3"),
        ("南京大学仙林校区", "3"),
        ("苏州", "4"),
        ("南京大学苏州校区", "4"),
        ("鼓楼校区", "1"),
        ("浦口", "2"),
        ("3", "3"),
    ],
)
def test_normalize_campus(text: str, code: str) -> None:
    assert school.normalize_campus(text) == code


@pytest.mark.parametrize("text", ["", "新校区", "不是校区", "火星校区"])
def test_normalize_campus_rejects_unknown(text: str) -> None:
    assert school.normalize_campus(text) is None


# ---------------------------------------------------------------- 教学楼


def test_normalize_building_known() -> None:
    code, note = school.normalize_building("3", "仙II区")
    assert code == "12"
    assert "JXLDM=12" in note


def test_normalize_building_tolerates_suffix() -> None:
    assert school.normalize_building("3", "仙II区3号楼")[0] == "12"


def test_normalize_building_unknown_is_empty_not_a_guess() -> None:
    """关键安全属性：认不出来就留空（= 随机），**绝不猜**。"""
    code, note = school.normalize_building("3", "一号楼")
    assert code == ""
    assert "随机" in note


def test_normalize_building_requires_campus() -> None:
    assert school.normalize_building(None, "仙II区")[0] == ""


def test_suzhou_buildings() -> None:
    assert school.normalize_building("4", "南雍楼")[0] == "S06"
    assert school.normalize_building("4", "公共教学楼")[0] == "S01"


# ---------------------------------------------------------------- 时间 → 节次


@pytest.mark.parametrize(
    ("start", "end", "span"),
    [
        ("08:00", "08:50", (1, 1)),
        ("08:30", "09:30", (1, 2)),
        ("16:10", "18:00", (7, 8)),
        ("16:00", "17:00", (7, 7)),  # 容忍 10 分钟零头
        ("11:00", "15:00", (4, 5)),  # 跨午休，一次申请覆盖
        ("11:30", "12:30", (4, 4)),
        ("13:00", "15:00", (5, 5)),  # 15:00 正好是第 6 节开始，不重叠（与旧《指导文档》一致）
        ("18:30", "20:20", (9, 10)),
        ("21:30", "22:20", (12, 12)),
    ],
)
def test_periods_for(start: str, end: str, span: tuple[int, int]) -> None:
    assert school.periods_for(start, end) == span


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("17:00", "16:00"),  # 填反
        ("12:30", "13:30"),  # 完全落在午休，对不上任何节次
        ("25:00", "26:00"),  # 非法
        ("", ""),
        ("08:00", "08:00"),  # 零长度
    ],
)
def test_periods_for_rejects(start: str, end: str) -> None:
    assert school.periods_for(start, end) is None


def test_period_label() -> None:
    assert school.period_label(7, 8) == "7-8"
    assert school.period_label(7, 7) == "7"


def test_period_table_is_twelve_monotonic_slots() -> None:
    assert sorted(school.PERIODS) == list(range(1, 13))
    for index, (start, end) in school.PERIODS.items():
        assert school.parse_time(start) < school.parse_time(end), f"第 {index} 节起止颠倒"


def test_periods_cover_exactly_the_bookable_window() -> None:
    assert school.PERIODS[1][0] == school.DAY_START
    assert school.PERIODS[12][1] == school.DAY_END


def test_lunch_break_has_no_periods() -> None:
    """午休时段不应有节次——这是「12:30-13:30 对不上任何节次」的根据。"""
    lunch_start = school.parse_time(school.LUNCH_START)
    lunch_end = school.parse_time(school.LUNCH_END)
    for start, end in school.PERIODS.values():
        s, e = school.parse_time(start), school.parse_time(end)
        assert not (s < lunch_end and lunch_start < e), f"{start}-{end} 侵入了午休"


# ---------------------------------------------------------------- 借用日期窗口


def test_bookable_range() -> None:
    lo, hi = school.bookable_range(date(2026, 9, 20))
    assert lo == date(2026, 9, 22)
    assert hi == date(2026, 9, 29)


def test_describe_day_range_is_human_readable() -> None:
    text = school.describe_day_range(date(2026, 9, 20))
    assert "2026-09-22" in text and "2026-09-29" in text
