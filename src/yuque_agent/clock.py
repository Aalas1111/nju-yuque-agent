"""统一的时钟：本项目所有「现在」一律按 **Asia/Shanghai** 算。

**为什么不能依赖服务器时区。** 这个项目的核心判定全都挂在「今天」上：

* 「申请周期 = 周六 00:00 ~ 下周五 23:59」；
* 「可借日期 = 今天 +2 天 ~ 今天 +9 天」（`prompts/polling.md` 里交给 LLM 判）；
* 「每周六 00:00 归档」。

而云服务器（阿里云 / 腾讯云 / 大多数 VPS）**默认就是 UTC**。用本地时间的话，
同一份代码在 UTC 机器上会：

1. **归档晚 8 小时**触发（北京周六 00:00 = UTC 周五 16:00，要等到 UTC 周六 00:00）；
2. 每天有 **8 小时窗口**「今天」算错一天——而这恰好是判定申请合不合规的依据。

所以这里把时区**钉死**：不管 `TZ` 环境变量、`/etc/localtime` 是什么，
:func:`now` 永远返回上海时间。**部署时不需要任何时区配置。**

为什么用 `ZoneInfo("Asia/Shanghai")` 而不是写死 `timezone(timedelta(hours=8))`：
前者说的是「要哪个**时区**」，后者说的是「偏移多少小时」。中国 1991 年后没有夏令时，
两者眼下完全等价；但前者语义正确，也留了余地。`tzdata` 已加进依赖，
所以 Windows 上（没有系统 IANA 库）也能用，行为与 Linux 一致。

**这条纪律由测试守着**：`tests/test_clock.py` 会扫描 `src/`，
发现 `clock.py` 之外还有人写 `datetime.now()` / `date.today()` 就直接失败。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ_NAME = "Asia/Shanghai"
"""本项目钉死的时区名。"""

TZ = ZoneInfo(TZ_NAME)
"""钉死的时区对象。测试里也该用它构造「现在」，否则测的就不是生产路径。"""


def now() -> datetime:
    """现在（**总是** Asia/Shanghai，不管服务器时区）。"""
    return datetime.now(TZ)


def today() -> date:
    """今天（Asia/Shanghai）。"""
    return now().date()


def stamp() -> str:
    """ISO 时间戳（秒精度），用于 session / 凭证这类落盘字段。"""
    return now().isoformat(timespec="seconds")


def compact_stamp(at: datetime | None = None) -> str:
    """``YYYYmmdd-HHMMSS`` 形式的时间戳（run_id 用）。"""
    return (at or now()).strftime("%Y%m%d-%H%M%S")


def server_tz_name() -> str:
    """服务器**系统**时区名（如 ``UTC`` / ``中国标准时间``）。

    **只用于自检展示**（``yqa doctor`` 里让你一眼确认「服务器就算是 UTC 也没关系」）。
    任何判断逻辑都不许用它——那正是本项目要摆脱的东西。

    放在这个模块里是因为它是全项目**唯一**允许碰系统时钟的地方，
    这样 :func:`tests.test_clock.test_no_code_bypasses_clock` 的源码扫描才是完整的。
    """
    return str(datetime.now().astimezone().tzinfo)
