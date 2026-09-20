"""时区纪律：本项目的「现在」一律 **Asia/Shanghai**，不跟服务器时区走。

背景：核心判定全挂在「今天」上（申请周期、可借日期 = 今天+2~+9 天、每周六 00:00 归档），
而云服务器默认是 UTC。用本地时间的话在 UTC 机器上会归档晚 8 小时、
且每天有 8 小时窗口「今天」算错一天。

所以时区被钉死在 `clock.py` 里，并由下面最后一条测试**扫源码**守住——
这类「约定容易在后来被人顺手破坏」的纪律，光写在文档里没用。
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from yuque_agent import clock

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "yuque_agent"


def test_timezone_is_pinned_to_shanghai() -> None:
    assert clock.TZ_NAME == "Asia/Shanghai"
    # 用的是命名时区，不是写死的 +08:00 偏移对象
    assert getattr(clock.TZ, "key", None) == "Asia/Shanghai", (
        "应当是 ZoneInfo('Asia/Shanghai')，而不是 timezone(timedelta(hours=8))"
    )
    assert clock.now().utcoffset() == timedelta(hours=8)


def test_now_is_the_same_instant_as_utc_but_expressed_in_shanghai() -> None:
    """钉死的是**表述时区**，不是把时钟拨快 8 小时。

    这条容易搞错：如果实现写成 `datetime.now() + timedelta(hours=8)`，
    绝对时刻就错了 8 小时，而 offset 看起来又是对的。所以必须比对绝对时刻。
    """
    cn = clock.now()
    utc = datetime.now(UTC)
    assert abs((cn - utc).total_seconds()) < 10, "绝对时刻应当是同一个瞬间"
    assert cn.utcoffset() == timedelta(hours=8)
    assert cn.astimezone(UTC) == pytest.approx(utc, abs=timedelta(seconds=10))


def test_now_ignores_the_TZ_environment_variable(monkeypatch) -> None:
    """`TZ` / `/etc/localtime` 被设成 UTC，也不能影响 clock。"""
    monkeypatch.setenv("TZ", "UTC")
    cn = clock.now()
    assert cn.utcoffset() == timedelta(hours=8)

    monkeypatch.setenv("TZ", "America/New_York")
    assert clock.now().utcoffset() == timedelta(hours=8)


def test_today_and_stamps_are_shanghai() -> None:
    assert clock.today() == clock.now().date()
    assert clock.stamp().endswith("+08:00")
    assert clock.compact_stamp() == clock.now().strftime("%Y%m%d-%H%M%S")
    at = datetime(2026, 9, 20, 10, 30, tzinfo=clock.TZ)
    assert clock.compact_stamp(at) == "20260920-103000"


def test_test_helpers_also_use_the_production_timezone() -> None:
    """`tests/` 里的 `dt()` 必须用生产时区构造时刻。

    否则测试跑在 UTC 的 CI 上时，注入的时刻与生产路径不是同一个时区——
    测出来的东西和线上不是一回事，这种「测试自身失真」比没有测试更危险。
    """
    from tests.test_debounce import dt as dt_debounce
    from tests.test_week import dt as dt_week

    for dt in (dt_week, dt_debounce):
        assert dt("2026-09-20T10:00").utcoffset() == timedelta(hours=8)


# ---------------------------------------------------------------- 源码守卫


def _bypasses_clock(node: ast.AST) -> str | None:
    """这段代码有没有绕开 clock 自己取时间？返回人话描述，没绕开就返回 None。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    # datetime.now(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "now"
        and isinstance(func.value, ast.Name)
        and func.value.id == "datetime"
    ):
        return "datetime.now()"
    # date.today(...)
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "today"
        and isinstance(func.value, ast.Name)
        and func.value.id == "date"
    ):
        return "date.today()"
    return None


def test_no_code_bypasses_clock() -> None:
    """**时区钉死的守卫**：`src/` 里除了 `clock.py`，谁都不许自己取「现在」。

    为什么要有这条：时区是「全项目一起守才有效」的约定。后来任何一处顺手写的
    `datetime.now()` 都会让那份逻辑重新跟着服务器时区走，而且**不会报错**——
    在开发机（+08:00）上永远看不出来，只在 UTC 服务器上错。

    用 AST 扫而不是文本扫：文档串里会引用 `datetime.now()` 作反例（比如
    `week.py` 那段「坑」的说明），文本扫会误伤。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "clock.py" or "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            what = _bypasses_clock(node)
            if what:
                offenders.append(f"{path.relative_to(SRC.parent.parent)}:{node.lineno} {what}")
    assert not offenders, (
        "这几处绕开了 clock，会让时区钉死失效（改从 `yuque_agent.clock` 取时间）：\n  "
        + "\n  ".join(offenders)
    )
