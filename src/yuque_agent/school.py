"""南京大学的「学校侧词汇表」：校区代码、教学楼代码、节次表。

**为什么这一层归程序，不归 LLM**：

* 校区名 → 代码、教学楼名 → `JXLDM`、`16:10-18:00` → 节次 `7-8`，
  全都是**查表/算术**，不是判断。「几种写法怎么办」那种问题（`9月16日`、`下午4点`、
  `仙林校区`）已经在 LLM 那边解决完了；到了结构化阶段就只剩确定性映射。
* 而且下游（教室借用插件 / `crb`）是**纯程序**，它要的是 `"3"`、`"11"` 这样的代码。
  我们如果递过去「仙II区」，查询会返回空、申请会静默失败。

**关于代码的来源与风险**：教学楼字典是学校接口**按校区现查**的
（`POST /jwapp/sys/kxjas/modules/kxjas/jxlcx.do`，body `XXXQDM=<校区代码>`）。
:data:`BUILDINGS` 是 2026-09-26 用登录态拉的全量（四个校区共 16 栋）。
认不出来时我们**宁可留空（= 随机）也不猜**——填一个错的教学楼代码，比不填危险得多。

**谁负责归一**：把社员的写法（「仙二」「仙2区」「逸夫楼A」）归到规范名**归 LLM**
——它比任何别名表都强；这份表会随每轮的变更报告下发（:func:`facts`），
它归一出来的名字就是这里的键。程序**只做「规范名 → JXLDM」这一步精确查表**，
不做任何模糊匹配（2026-09-27 清掉了原先的 `_canon_name` 安全网）：
变体匹配会把「LLM 没归好」这件事悄悄藏起来，而查不到就留空（= 随机）更诚实、
也更好排查。详见 `docs/design.md` §7.2。

**怎么重查这张表**（需要南大统一认证的登录态，OpenAPI 拿不到；每学期核一次）：

```bash
# 上一版项目 crb（归档：Archived/NJU_Classroom_Booking）——字典现查现出
crb login                                   # 扫码（一次）
crb buildings --campus 3 --json             # → [{"JXLDM":"11","JXLMC":"仙I区"}, …]
```

把返回的 `JXLMC` / `JXLDM` 填进 :data:`BUILDINGS` 对应校区即可（键用 JXLMC 原样）。
表的键必须与学校系统里的写法**逐字一致**（含罗马数字写法）——因为查表是精确的。

校方规则（来自 谷和平 对借用页面的实测）：

* 可借日期 = **今天 +2 ~ 今天 +9 天**（最多提前 9 天，至少提前 2 天）；
* 借用时间必须在 **08:00 – 22:20**；
* **12:00 – 14:00 是午休**，本来就不需要借教室。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

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

#: ``校区代码 -> {规范教学楼名: JXLDM}``。**全量**（2026-09-26 用登录态从学校接口现查）。
#:
#: 「规范名」= 学校系统里那一列 `JXLMC` **原样**（连罗马数字的写法也是）。
#: 这份表会随每轮的变更报告一起下发给 LLM（见 :func:`facts`），
#: 它归一出来的名字**必然**是这里的键——所以查表是精确的，不需要变体匹配。
#: 键必须与学校系统逐字一致：`仙II区`（拉丁 II）与 `仙Ⅱ区`（罗马 Ⅱ）是**两个**字符串。
#:
#: 重查办法见模块 docstring 末尾（需要南大统一认证；每学期开学后值得核一次）。
BUILDINGS: dict[str, dict[str, str]] = {
    "1": {  # 鼓楼
        "教学楼": "1",
        "逸夫馆": "8",
        "逸夫管理科学楼": "10",
        "新教学楼": "20",
        "费彝民楼": "31",
        "南教": "32",
    },
    "2": {  # 浦口
        "思源图书馆": "30",
    },
    "3": {  # 仙林
        "仙I区": "11",
        "仙II区": "12",
        "逸夫楼A区": "15",
        "逸夫楼B区": "16",
        "逸夫楼C区": "17",
        "图书馆": "18",
        "环科楼": "111",
    },
    "4": {  # 苏州
        "南雍楼": "S06",
        "公共教学楼": "S01",
    },
}


#: 各校区**教室名**的写法规则与真实样例（2026-09-26 从学校接口「空闲教室查询」抽样）。
#:
#: 为什么要单独记：下游（crb `planner.py:182`）对意向教室是**精确字符串匹配**——
#: 差一个字符（拉丁 I vs 罗马 Ⅰ、半角/全角、有无连字符）就**静默退化成随机教室**。
#: 样例只用来**锚定写法**（取自某一时段的空闲教室，不是全集）。
ROOM_NOTATION: dict[str, dict[str, Any]] = {
    "1": {
        "rule": "楼简称 + 房间号（`馆2-101`、`教115`、`新教-203`），数字是半角",
        "examples": ["教115", "馆2-101", "新教-203"],
        "caveat": "「专用教室」是占位名、不唯一，别指定",
    },
    "2": {
        "rule": "楼简称 + 方位 + 房间号（`图东101`），通常没有连字符",
        "examples": ["图东101", "图东407"],
    },
    "3": {
        "rule": "楼简称 + **真罗马数字**（`Ⅰ` U+2160 / `Ⅱ` U+2161，**不是拉丁 I**）+ `-` + 房间号",
        "examples": ["仙Ⅰ-410", "仙Ⅱ-218", "逸B-506", "图书馆124"],
    },
    "4": {
        "rule": "楼简称 + 方位/字母 + 房间号（`南雍-东108`、`苏教A209`）",
        "examples": ["南雍-东108", "苏教A209"],
    },
}


def facts() -> dict[str, Any]:
    """给 LLM 的「学校侧事实」，随每轮的变更报告下发（**不抄进提示词**）。

    为什么下发这一份：**把社员的写法归一到规范名是 LLM 的活**（它比任何别名表都强），
    但归一目标必须是**程序/下游认得的那几个名字**。所以两边共用同一份表——
    提示词只说「归一成 `school.buildings` / `school.rooms` 里的写法」，程序
    （:func:`normalize_building`）再把规范名换成 JXLDM。这样「LLM 归一 → 程序查表」不可能对不上。
    """
    return {
        "campuses": dict(CAMPUS_CODES),
        "buildings": {campus: dict(table) for campus, table in BUILDINGS.items()},
        "rooms": {campus: dict(note) for campus, note in ROOM_NOTATION.items()},
    }


def normalize_building(campus_code: str | None, text: str) -> tuple[str, str]:
    """返回 ``(JXLDM, 说明)``。认不出来时 ``JXLDM`` 为空串 = 交给下游随机。

    **只做精确查表**：``text`` 要么是 :data:`BUILDINGS` 里的规范名，要么就是 JXLDM 本身。

    为什么不做模糊匹配（2026-09-27 删掉了原来的那一套）：写法归一**归 LLM**
    （它拿到的变更报告里就带着这张表，归一目标与这里的键必然一致）。
    程序再去猜「仙二」是不是「仙II区」，等于把 LLM 判断的正确性用一份别名表
    又赌了一遍——猜错就把下游引到**另一栋**教学楼，而查不到留空只是退化成随机。
    「宁可留空也不猜」是这一层的安全属性。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "未填教学楼，按随机处理"
    table = BUILDINGS.get(campus_code or "", {})
    if not table:
        return "", f"「{raw}」所在校区没有教学楼代码表（留空 = 随机）"

    code = table.get(raw)
    if code is not None:
        return code, f"教学楼「{raw}」→ JXLDM={code}"
    if raw in table.values():  # 直接给了 JXLDM（如「12」）也认
        return raw, f"教学楼「{raw}」看起来就是 JXLDM，直接用"
    return "", f"教学楼「{raw}」不是表里的规范名（留空 = 随机；下游可自行解析）"


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
