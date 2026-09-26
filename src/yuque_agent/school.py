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
它归一出来的名字就是这里的键。程序只做**规范名 → JXLDM** 这一步机械映射；
:func:`_canon_name` 那套归一留着当安全网（LLM 万一又塞了个变体进来）。
详见 `docs/design.md` §7.2。

**怎么重查这张表**（需要南大统一认证的登录态，OpenAPI 拿不到；每学期核一次）：

```bash
# 上一版项目 crb（归档：Archived/NJU_Classroom_Booking）——字典现查现出
crb login                                   # 扫码（一次）
crb buildings --campus 3 --json             # → [{"JXLDM":"11","JXLMC":"仙I区"}, …]
```

把返回的 `JXLMC` / `JXLDM` 填进 :data:`BUILDINGS` 对应校区即可（键用规范名 + JXLDM）。
数字的各种写法（`Ⅰ`/`1`/`一`、`II`/`2`/`二`、全角）不用登记，:func:`_canon_name` 会归一。

校方规则（来自 谷和平 对借用页面的实测）：

* 可借日期 = **今天 +2 ~ 今天 +9 天**（最多提前 9 天，至少提前 2 天）；
* 借用时间必须在 **08:00 – 22:20**；
* **12:00 – 14:00 是午休**，本来就不需要借教室。
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Any

#: 教学楼名里的中文数字 → 阿拉伯数字（归一用，见 :func:`_canon_name`）。
_CJK_DIGITS = {
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}

#: 拉丁（NFKC 折过罗马数字之后的）数字 → 阿拉伯数字。
_ROMAN_DIGITS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "ix": 9, "x": 10}

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
#: 「规范名」= 学校系统里那一列 `JXLMC` 原样；数字的各种写法（Ⅰ/1/一、II/2/二、全角）
#: 不用登记，:func:`_canon_name` 会归一，而且这份表会随每轮的变更报告一起下发给 LLM
#: （见 :func:`facts`），所以它归一出来的名字**必然**是这里的键。
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


def facts() -> dict[str, Any]:
    """给 LLM 的「学校侧事实」，随每轮的变更报告下发（**不抄进提示词**）。

    为什么下发这一份：**把社员的写法归一到规范名是 LLM 的活**（它比任何别名表都强），
    但归一目标必须是**程序认得的那几个名字**。所以两边共用同一份表——
    提示词只说「归一成 `school.buildings` 里的名字」，程序（:func:`normalize_building`）
    再把规范名换成 JXLDM。这样「LLM 归一 → 程序查表」不可能对不上。
    """
    return {
        "campuses": dict(CAMPUS_CODES),
        "buildings": {campus: dict(table) for campus, table in BUILDINGS.items()},
    }


def _canon_name(text: str) -> str:
    """把教学楼名里的**数字写法**归一，便于比对。

    先 NFKC（全角 → 半角、罗马数字 Ⅰ Ⅱ → 拉丁 I I），再把拉丁/中文数字折成阿拉伯数字，
    并去掉空格、统一大小写。于是社员写的这些写法都会落到同一个键上::

        仙I区 · 仙Ⅰ区 · 仙1区 · 仙一区 · 仙i区 · 仙　I 区  →  仙1区
        逸夫楼A区 · 逸夫楼a区                                →  逸夫楼a区

    为什么要它：学校的教学楼字典是按校区现查的（见 `docs/design.md` D2），
    每换一学期/一栋楼就得往表里加名字；靠「手工枚举所有写法」必漏，
    而归一之后**只需登记学校系统里的那个规范名**，常见变体自动覆盖。
    """
    flat = unicodedata.normalize("NFKC", text or "").replace(" ", "").lower()
    flat = "".join(_CJK_DIGITS.get(ch, ch) for ch in flat)
    # 拉丁数字 → 阿拉伯数字（只在两侧不是字母时替换，别把单词里的 i/v/x 吃掉）
    flat = re.sub(
        r"(?<![a-z])(x{1,3}|ix|iv|v|i{1,3})(?![a-z])",
        lambda m: str(_ROMAN_DIGITS[m.group(1)]),
        flat,
    )
    # 末尾的「区」不算区别（社员常常不写：「逸夫楼A」=「逸夫楼A区」）
    return flat[:-1] if flat.endswith("区") and len(flat) > 1 else flat


def normalize_building(campus_code: str | None, text: str) -> tuple[str, str]:
    """返回 ``(JXLDM, 说明)``。认不出来时 ``JXLDM`` 为空串 = 交给下游随机。

    「宁可留空也不猜」：一个错的教学楼代码会让下游去查错的教学楼，
    而留空只是退化成「在同一校区里随机」。

    比对前两边都过 :func:`_canon_name`（数字写法归一），所以
    「仙二区」「仙2区」「仙Ⅱ区」都能命中表里的「仙I区/仙II区」；表里没有的仍然留空。
    """
    raw = (text or "").strip()
    if not raw:
        return "", "未填教学楼，按随机处理"
    table = BUILDINGS.get(campus_code or "", {})
    if not table:
        return "", f"「{raw}」所在校区没有教学楼代码表（留空 = 随机）"

    if raw in table.values():  # 直接给了 JXLDM（如「12」）也认
        return raw, f"教学楼「{raw}」看起来就是 JXLDM，直接用"

    want = _canon_name(raw)
    for name, code in table.items():
        if _canon_name(name) == want:
            return code, f"教学楼「{raw}」→ JXLDM={code}"
    # 容忍「仙II区3号楼」这类带尾巴的写法。长的名字优先，避免短名把长名截胡。
    for name, code in sorted(table.items(), key=lambda kv: len(kv[0]), reverse=True):
        if _canon_name(name) in want:
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
