"""南京大学的「学校侧词汇表」：校区代码、教学楼代码、节次表。

**为什么这一层归程序，不归 LLM**：

* 校区名 → 代码、教学楼名 → `JXLDM`、`16:10-18:00` → 节次 `7-8`，
  全都是**查表/算术**，不是判断。「几种写法怎么办」那种问题（`9月16日`、`下午4点`、
  `仙林校区`）已经在 LLM 那边解决完了；到了结构化阶段就只剩确定性映射。
* 而且下游（教室借用插件 / `crb`）是**纯程序**，它要的是 `"3"`、`"11"` 这样的代码。
  我们如果递过去「仙II区」，查询会返回空、申请会静默失败。

**关于代码的来源与风险**：教学楼代码表来自上一版项目对学校接口
（`/jwapp/sys/kxjas/modules/kxjas/jxlcx.do`）的实测记录 + `crb` 的实测输出，
**只覆盖已知的几个**。认不出来时我们**宁可留空（= 随机）也不猜**——
填一个错的教学楼代码，比不填危险得多。

校方规则（来自 谷和平 对借用页面的实测）：

* 可借日期 = **今天 +2 ~ 今天 +9 天**（最多提前 9 天，至少提前 2 天）；
* 借用时间必须在 **08:00 – 22:20**；
* **12:00 – 14:00 是午休**，本来就不需要借教室。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# ---------------------------------------------------------------- 校区

CAMPUS_CODES: dict[str, str] = {
    "1": "鼓楼",
    "2": "浦口",
    "3": "仙林",
    "4": "苏州",
}

_CAMPUS_ALIASES: dict[str, str] = {}
for _code, _name in CAMPUS_CODES.items():
    for _alias in (_code, _name, f"{_name}校区", f"南京大学{_name}校区", f"{_name}区"):
        _CAMPUS_ALIASES[_alias] = _code
# 常见口头简称
_CAMPUS_ALIASES.update({"鼓楼校区": "1", "苏州校区": "4", "浦口": "2"})


def normalize_campus(text: str) -> str | None:
    """把校区的各种写法归一成代码；认不出来返回 ``None``。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw in _CAMPUS_ALIASES:
        return _CAMPUS_ALIASES[raw]
    for alias, code in _CAMPUS_ALIASES.items():
        if len(alias) > 1 and alias in raw:
            return code
    return None


# ---------------------------------------------------------------- 教学楼

#: ``校区代码 -> {教学楼名: JXLDM}``。**部分覆盖**，来源见模块 docstring。
BUILDINGS: dict[str, dict[str, str]] = {
    "3": {  # 仙林（上一版实测 jxlcx.do 的记录）
        "仙I区": "11",
        "仙Ⅰ区": "11",
        "仙1区": "11",
        "仙II区": "12",
        "仙Ⅱ区": "12",
        "仙2区": "12",
        "逸夫楼A区": "15",
        "逸夫楼B区": "16",
    },
    "4": {  # 苏州（来自 crb 的实测输出）
        "南雍楼": "S06",
        "公共教学楼": "S01",
    },
}


def normalize_building(campus_code: str | None, text: str) -> tuple[str, str]:
    """返回 ``(JXLDM, 说明)``。认不出来时 ``JXLDM`` 为空串 = 交给下游随机。

    「宁可留空也不猜」：一个错的教学楼代码会让下游去查错的教学楼，
    而留空只是退化成「在同一校区里随机」。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "未填教学楼，按随机处理"
    table = BUILDINGS.get(campus_code or "", {})
    for name, code in table.items():
        if name == raw:
            return code, f"教学楼「{raw}」→ JXLDM={code}"
    # 容忍「仙II区3号楼」这类带尾巴的写法
    for name, code in table.items():
        if name in raw:
            return code, f"教学楼「{raw}」按「{name}」解析 → JXLDM={code}"
    return "", f"教学楼「{raw}」不在已知代码表里（留空 = 随机；下游可自行解析）"


# ---------------------------------------------------------------- 节次

#: 节次 -> (开始, 结束)，全校统一。
PERIODS: dict[int, tuple[str, str]] = {
    1: ("08:00", "08:50"),
    2: ("09:00", "09:50"),
    3: ("10:10", "11:00"),
    4: ("11:10", "12:00"),
    5: ("14:00", "14:50"),
    6: ("15:00", "15:50"),
    7: ("16:10", "17:00"),
    8: ("17:10", "18:00"),
    9: ("18:30", "19:20"),
    10: ("19:30", "20:20"),
    11: ("20:30", "21:20"),
    12: ("21:30", "22:20"),
}

DAY_START = "08:00"
DAY_END = "22:20"
LUNCH_START = "12:00"
LUNCH_END = "14:00"

MIN_DAYS_AHEAD = 2
"""至少提前 2 天（校方规则：可借日期从 今天+2 开始）。"""

MAX_DAYS_AHEAD = 9
"""最多提前 9 天（校方规则）。"""

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_time(text: str) -> int | None:
    """``"16:10"`` → 分钟数（970）；非法返回 ``None``。"""
    match = _TIME_RE.match((text or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def periods_for(start: str, end: str) -> tuple[int, int] | None:
    """把 ``HH:MM`` 区间映射成节次区间；映射不出来返回 ``None``。

    规则：取**所有与请求区间有重叠**的节次，返回最小~最大。
    这样 ``16:00-17:00`` → 第 7 节（容忍 10 分钟零头），
    ``11:00-15:00`` → 第 4-5 节（跨午休，一次申请覆盖）。
    """
    s = parse_time(start)
    e = parse_time(end)
    if s is None or e is None or s >= e:
        return None
    hit = [
        index
        for index, (ps, pe) in PERIODS.items()
        if (parse_time(ps) or 0) < e and s < (parse_time(pe) or 0)
    ]
    if not hit:
        return None
    return min(hit), max(hit)


def period_label(ksjc: int, jsjc: int) -> str:
    """``(7, 8)`` → ``"7-8"``；``(7, 7)`` → ``"7"``。"""
    return f"{ksjc}-{jsjc}" if ksjc != jsjc else str(ksjc)


def bookable_range(today: date) -> tuple[date, date]:
    """校方允许的借用日期范围（闭区间）。"""
    return today + timedelta(days=MIN_DAYS_AHEAD), today + timedelta(days=MAX_DAYS_AHEAD)


def describe_day_range(today: date) -> str:
    lo, hi = bookable_range(today)
    return f"{lo.isoformat()} ~ {hi.isoformat()}（今天 {today.isoformat()} 起 +{MIN_DAYS_AHEAD} ~ +{MAX_DAYS_AHEAD} 天）"
