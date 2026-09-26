"""学校侧词汇表：校区代码、教学楼代码、时间→节次。

这一层是「程序负责测量、LLM 负责判断」里的**测量**那一半。它必须**确定、可测**——
因为下游（教室借用插件）是纯程序，一个错的教学楼代码会让它去查错的教学楼。

楼名的**写法归一归 LLM**（字典随变更报告下发，见 `school.facts`），
程序只做**精确查表**。2026-09-27 清掉了原先那套模糊匹配（数字写法归一 / 末尾容错 /
长名优先）——它会把「LLM 没归好」悄悄兜住，而兜错就会指到另一栋楼。
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


def test_normalize_building_is_an_exact_lookup() -> None:
    """规范名照抄能命中——这是「LLM 归一 → 程序查表」里程序的那一半。"""
    assert school.normalize_building("3", "仙II区")[0] == "12"
    assert school.normalize_building("3", "逸夫楼A区")[0] == "15"


@pytest.mark.parametrize(
    "written",
    [
        "仙二",
        "仙二区",
        "仙2区",
        "仙Ⅱ区",  # 罗马数字 Ⅱ（U+2161）：规范化写法是拉丁 II，这是**另一个**字符串
        "仙ii区",
        "仙1区",
        "仙一区",
        "逸夫楼A",
        "逸夫楼a区",
        "仙II区3号楼",
    ],
)
def test_variant_spellings_are_left_to_the_llm(written: str) -> None:
    """写法变体在程序这一层**不命中**（留空 = 随机）。

    归一归 LLM：它拿到的变更报告里带着 `school.BUILDINGS`，归一目标与这里的键
    必然一致。程序再猜一遍只会把「LLM 没归好」这件事藏起来——
    猜错就是把下游引到**另一栋**教学楼（2026-09-27 按负责人意见删掉模糊匹配）。
    """
    code, note = school.normalize_building("3", written)
    assert code == "", f"{written!r} 被程序猜出来了（{note}）——变体应由 LLM 归一"


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


def test_building_table_covers_all_four_campuses() -> None:
    """四个校区都要有表（2026-09-26 用登录态从学校接口拉的全量：共 16 栋）。

    为什么钉住：这张表是**现查**来的（见 `school` 模块 docstring）。
    谁删/漏一行，社员写了那栋楼就会被当成「认不出来」→ 白白退化成随机。
    """
    assert set(school.BUILDINGS) == set(school.CAMPUS_CODES) == {"1", "2", "3", "4"}
    assert {c: len(t) for c, t in school.BUILDINGS.items()} == {"1": 6, "2": 1, "3": 7, "4": 2}


def test_building_codes_match_the_school_dictionary() -> None:
    """JXLDM 必须与学校字典逐个一致（错代码会让下游去查**错的**教学楼）。"""
    assert school.BUILDINGS["3"] == {
        "仙I区": "11",
        "仙II区": "12",
        "逸夫楼A区": "15",
        "逸夫楼B区": "16",
        "逸夫楼C区": "17",
        "图书馆": "18",
        "环科楼": "111",
    }
    assert school.BUILDINGS["1"] == {
        "教学楼": "1",
        "逸夫馆": "8",
        "逸夫管理科学楼": "10",
        "新教学楼": "20",
        "费彝民楼": "31",
        "南教": "32",
    }
    assert school.BUILDINGS["2"] == {"思源图书馆": "30"}
    assert school.BUILDINGS["4"] == {"南雍楼": "S06", "公共教学楼": "S01"}


def test_normalize_accepts_a_bare_jxldm() -> None:
    """直接把 JXLDM 递进来也认（别把它当成「认不出来的名字」）。"""
    assert school.normalize_building("3", "12")[0] == "12"
    assert school.normalize_building("1", "31")[0] == "31"


def test_facts_carry_the_same_table_the_program_uses() -> None:
    """下发给 LLM 的 facts 必须与程序查表用的是**同一份**。

    否则「LLM 归一出来的名字 → 程序查表」会在两个版本的表之间对不上。
    """
    facts = school.facts()
    assert facts["buildings"] == school.BUILDINGS
    assert facts["campuses"]["3"] == "仙林"


def test_known_building_table_is_well_formed() -> None:
    """表里不许有空的标题/代码，校区键必须是真实校区。"""
    for campus, table in school.BUILDINGS.items():
        assert campus in school.CAMPUS_CODES, f"未知校区键：{campus}"
        for name, code in table.items():
            assert name.strip() and code.strip(), f"{campus}/{name!r} 有空字段"


def test_room_notation_is_shipped_to_the_llm() -> None:
    """各校区教室名的写法规则/样例必须随 facts 下发（下游精确匹配，差一字符就退随机）。"""
    rooms = school.facts()["rooms"]
    assert set(rooms) == {"1", "2", "3", "4"}
    for campus, note in rooms.items():
        assert note["rule"].strip() and note["examples"], f"校区 {campus} 缺写法规则或样例"
    # 仙林那一条最容易踩：真罗马数字 U+2160/U+2161，不是拉丁 I
    assert "Ⅰ" in rooms["3"]["examples"][0] and "I" not in rooms["3"]["examples"][0][:1]
    assert "仙Ⅰ-410" in rooms["3"]["examples"]


def test_room_notation_does_not_leak_a_room_type() -> None:
    """教室类型字典（智慧研讨 11 等）用户明确「暂时不做」——别顺手加进来。"""
    assert "教室类型" not in str(school.facts())
