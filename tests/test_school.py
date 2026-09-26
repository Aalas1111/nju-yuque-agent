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
        # ---- 边界：正好贴上某一节的边，**不算重叠** ----
        # 需求方实地问过的两个例子：
        ("08:00", "10:10", (1, 2)),  # 10:10 正好是第 3 节开始 → 不含第 3 节
        ("08:50", "10:00", (2, 2)),  # 08:50 正好是第 1 节结束 → 不含第 1 节
        ("08:00", "09:00", (1, 1)),  # 09:00 正好是第 2 节开始 → 不含第 2 节
        # 但只要**越过**边界一点点，整节都算上（容忍零头，宁可借多）：
        ("08:00", "09:10", (1, 2)),
        ("11:50", "14:30", (4, 5)),  # 跨午休，两头各覆盖一节
        ("18:00", "19:00", (9, 9)),  # 18:00-18:30 是课间，只落到第 9 节
    ],
)
def test_periods_for(start: str, end: str, span: tuple[int, int]) -> None:
    assert school.periods_for(start, end) == span


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("17:00", "16:00"),  # 填反
        ("12:30", "13:30"),  # 完全落在午休，对不上任何节次
        ("08:50", "09:00"),  # 完全落在课间空档（不是午休，但同样不排课）
        ("09:50", "10:10"),  # 同上
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


def test_gaps_between_periods_need_no_classroom() -> None:
    """节与节之间的课间也不排课——身处课间的申请同样对不上任何节次。

    需求方原话：「节次之间的空档比如下课时间 / 吃饭时间不在考虑范围内，
    这些时间段不需要借教室」。所以这不是缺陷，是学校本来的规则。
    提示词里对应有一条「完全落在课间 → 退回」的例子。
    """
    ordered = [school.PERIODS[i] for i in sorted(school.PERIODS)]
    for (_, prev_end), (next_start, _) in zip(ordered, ordered[1:], strict=False):
        if prev_end == next_start:
            continue  # 无缝相接，没有空档
        s, e = school.parse_time(prev_end), school.parse_time(next_start)
        assert s is not None and e is not None and s < e
        # 身处这段空档的申请，对不上任何节次
        assert school.periods_for(prev_end, next_start) is None, (
            f"{prev_end}-{next_start} 是课间空档，不该映射出节次"
        )


# ---------------------------------------------------------------- 借用日期窗口


def test_bookable_range() -> None:
    lo, hi = school.bookable_range(date(2026, 9, 20))
    assert lo == date(2026, 9, 22)
    assert hi == date(2026, 9, 29)


# ---------------------------------------------------------------- 写法覆盖


@pytest.mark.parametrize(
    "written",
    [
        "仙I区",
        "仙Ⅰ区",
        "仙1区",
        "仙一区",
        "仙i区",
        "仙 I 区",
        "仙２区",  # 第 I 区
        "仙II区",
        "仙Ⅱ区",
        "仙2区",
        "仙二区",
        "仙二",
        "仙ii区",  # 第 II 区
        "仙II区3号楼",  # 带尾巴
    ],
)
def test_building_numeric_variants_resolve(written: str) -> None:
    """数字的各种写法都要能落到同一个 JXLDM（归一在 `school._canon_name`）。

    为什么重要：学校的教学楼字典是**按校区现查**的、还会随学期变，
    靠「手工枚举所有写法」必漏；归一之后只登记学校系统里的规范名，
    社员写「仙二」「仙2区」「仙Ⅱ区」都能命中。
    """
    code, note = school.normalize_building("3", written)
    assert code in {"11", "12"}, f"{written!r} 没认出来：{note}"


def test_building_case_and_missing_suffix_variants() -> None:
    assert school.normalize_building("3", "逸夫楼A")[0] == "15"
    assert school.normalize_building("3", "逸夫楼a区")[0] == "15"
    assert school.normalize_building("3", "逸夫楼B区")[0] == "16"


def test_campus_without_a_building_table_does_not_guess() -> None:
    """鼓楼(1)/浦口(2) 还没有字典（要登录学校接口现查）→ 留空（= 随机），不猜。"""
    for campus in ("1", "2"):
        code, note = school.normalize_building(campus, "任何楼")
        assert code == "", f"校区 {campus} 没有表时不该猜出代码：{note}"
        assert "随机" in note


def test_known_building_table_is_well_formed() -> None:
    """表里不许有空的标题/代码，校区键必须是真实校区。"""
    for campus, table in school.BUILDINGS.items():
        assert campus in school.CAMPUS_CODES, f"未知校区键：{campus}"
        for name, code in table.items():
            assert name.strip() and code.strip(), f"{campus}/{name!r} 有空字段"
