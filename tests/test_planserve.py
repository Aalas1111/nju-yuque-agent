"""带密钥的 ``plan.json`` 下载口。

这是整个项目里**唯一对外开的网络口**，而且是明文 HTTP，所以测试要比别处狠：
不只是「能下」，还要把「不能下」的那些路都堵一遍 —— 路径越狱、无密钥、
错密钥、锁死、以及**密钥绝不能进日志**（日志是给人看的，泄进去就等于泄到 journald 里）。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from yuque_agent import planserve
from yuque_agent.config import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws", plan_key="correct-horse")
    s.ensure_dirs()
    s.plan_file.parent.mkdir(parents=True, exist_ok=True)
    s.plan_file.write_text(
        json.dumps({"cycle": "0919-0925", "activities": [{"date": "2026-09-23"}]}, indent=2),
        encoding="utf-8",
    )
    return s


@pytest.fixture()
def server(settings: Settings):
    settings.plan_port = 0  # 让内核挑一个空闲端口
    httpd = planserve.build_server(settings, host="127.0.0.1")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str, *, key: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url)
    if key is not None:
        req.add_header("X-Plan-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def get_q(url: str, key: str) -> tuple[int, bytes, dict[str, str]]:
    return get(f"{url}/plan.json?key={key}")


# -- 失败关闭 --------------------------------------------------------------


def test_refuses_to_start_without_a_key(settings: Settings) -> None:
    """没配密钥就不开张 —— 宁可不开，也不能开一个不要密钥的下载口。"""
    settings.plan_key = ""
    with pytest.raises(RuntimeError, match="YQA_PLAN_KEY"):
        planserve.build_server(settings, host="127.0.0.1")


# -- 正常取件 --------------------------------------------------------------


def test_serves_the_plan_with_the_right_key(settings: Settings, server: str) -> None:
    status, body, headers = get_q(server, "correct-horse")
    assert status == 200
    assert json.loads(body)["cycle"] == "0919-0925"
    assert "application/json" in headers["Content-Type"]


def test_served_bytes_are_identical_to_the_file(settings: Settings, server: str) -> None:
    """必须是**原字节**——中间做任何 JSON 往返都可能改变下游读到的东西。"""
    status, body, _ = get_q(server, "correct-horse")
    assert status == 200
    assert body == settings.plan_file.read_bytes()


def test_key_via_header_also_works(settings: Settings, server: str) -> None:
    status, body, _ = get(f"{server}/plan.json", key="correct-horse")
    assert status == 200
    assert b"0919-0925" in body


def test_download_is_never_cached(settings: Settings, server: str) -> None:
    """cac 拿到的必须是**此刻**那份。

    一旦中间缓存了，他会提交一个上周的清单 —— 而这里谁都看不出来。
    """
    _, _, headers = get_q(server, "correct-horse")
    assert "no-store" in headers.get("Cache-Control", "")


def test_form_page_needs_no_key_and_shows_no_data(settings: Settings, server: str) -> None:
    status, body, _ = get(f"{server}/")
    assert status == 200
    text = body.decode("utf-8")
    assert "密钥" in text
    # 页面里只有例子，不能有**真实清单的内容**
    assert "2026-09-23" not in text, "表单页不该泄露清单内容"
    assert "activities" not in text


def test_healthz_is_keyless_and_contentless(server: str) -> None:
    status, body, _ = get(f"{server}/healthz")
    assert status == 200 and body.strip() == b"ok"


# -- 挡住的部分 ------------------------------------------------------------


def test_wrong_key_is_rejected(settings: Settings, server: str) -> None:
    status, body, _ = get_q(server, "wrong")
    assert status == 401
    assert b"0919-0925" not in body


def test_missing_key_is_rejected(settings: Settings, server: str) -> None:
    status, body, _ = get(f"{server}/plan.json")
    assert status == 401
    assert b"0919-0925" not in body


def test_empty_key_never_matches_even_if_configured_empty(tmp_path: Path) -> None:
    """就算配置成空串（理论上到不了这儿），空 key 也不许换到文件。"""
    s = Settings(repo="g/kb", workspace=tmp_path / "ws", plan_key="")
    s.ensure_dirs()
    s.plan_file.write_text("{}", encoding="utf-8")
    s.plan_port = 0
    with pytest.raises(RuntimeError):
        planserve.build_server(s, host="127.0.0.1")


@pytest.mark.parametrize(
    "path",
    [
        "/../../../etc/passwd",
        "/plan.json/../../../../etc/passwd",
        "/outbox/plan.defaults.json",
        "/../../plan.defaults.json",
        "/archive/../../plan.json",
        "/archive/..%2f..%2fplan.json",
        "/archive/0919-0925/../../../plan.json",
        "/archive/0919-0925/applications/index.json",
    ],
)
def test_path_traversal_and_neighbours_are_404(server: str, path: str) -> None:
    """只管那一个文件。

    特别地 ``plan.defaults.json``（借用人姓名/电话）就在隔壁，绝不能因为
    「也在 outbox 下」就被顺手发出去。
    """
    status, body, _ = get(f"{server}{path}?key=correct-horse")
    assert status == 404, f"{path} 竟然返回 {status}"
    assert b"JYRXM" not in body


def test_malformed_cycle_is_404(settings: Settings, server: str) -> None:
    status, _, _ = get_q(f"{server}/archive/not-a-cycle/plan.json", "correct-horse")
    assert status == 404


def test_archive_cycle_is_served_when_it_exists(settings: Settings, server: str) -> None:
    dest = settings.cycle_archive_dir("0912-0918")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "plan.json").write_text('{"cycle": "0912-0918"}', encoding="utf-8")
    status, body, _ = get(f"{server}/archive/0912-0918/plan.json?key=correct-horse")
    assert status == 200
    assert json.loads(body)["cycle"] == "0912-0918"


def test_archive_via_query_helper_does_not_double_the_filename(server: str) -> None:
    """`get_q` 会自己拼 /plan.json —— 归档路径不能重复拼（自身踩过的坑）。"""
    assert get_q(server, "correct-horse")[0] == 200
    assert get(f"{server}/archive/0919-0925/plan.json/plan.json?key=correct-horse")[0] == 404


def test_missing_plan_says_so_instead_of_404_silently(settings: Settings, server: str) -> None:
    settings.plan_file.unlink()
    status, body, _ = get_q(server, "correct-horse")
    assert status == 404
    assert "还没有清单" in body.decode("utf-8")


# -- 暴力猜 ---------------------------------------------------------------


def test_repeated_failures_get_locked_out(settings: Settings, server: str) -> None:
    for _ in range(planserve._MAX_FAILURES):
        assert get_q(server, "nope")[0] == 401
    status, body, _ = get_q(server, "nope")
    assert status == 429
    # 锁住之后，**正确**密钥也进不来（锁的是来源，不是那一次请求）
    assert get_q(server, "correct-horse")[0] == 429


def test_lockout_is_per_source(server: str) -> None:
    """锁的是**来源 IP**，不是全局 —— 一个扫描器不该把 cac 也挡在门外。"""
    for _ in range(planserve._MAX_FAILURES):
        get_q(server, "nope")
    assert get_q(server, "correct-horse")[0] == 429  # 本机这个来源被锁了

    # 状态表本身按来源分键：没试过的来源不受影响
    fresh = planserve._State()
    assert fresh.locked("10.0.0.9") is False
    fresh.record_failure("10.0.0.9")
    assert fresh.locked("10.0.0.9") is False
    assert fresh.locked("10.0.0.10") is False


def test_lockout_window_expires() -> None:
    """锁不是永久的（否则一次误操作就把人永久关在外面）。"""
    state = planserve._State()
    for _ in range(planserve._MAX_FAILURES):
        state.record_failure("1.2.3.4")
    assert state.locked("1.2.3.4") is True
    # 把失败时间往前拨到窗口之外
    state._fails["1.2.3.4"] = [t - planserve._WINDOW - 1 for t in state._fails["1.2.3.4"]]
    assert state.locked("1.2.3.4") is False


# -- 日志里不许有密钥 ------------------------------------------------------


def test_key_never_appears_in_the_log(settings: Settings, server: str, capsys) -> None:
    """日志进 journald，泄进去就等于泄出去。

    默认的 ``BaseHTTPRequestHandler.log_message`` 会打完整 URL，而密钥在
    ``?key=`` 里 —— 所以我们必须覆盖掉它。
    """
    get_q(server, "correct-horse")
    get_q(server, "wrong-key-please")
    get(f"{server}/plan.json")
    out = capsys.readouterr()
    everything = out.out + out.err
    assert "correct-horse" not in everything
    assert "wrong-key-please" not in everything
    assert "key=" not in everything
    # 但事件本身要留下（否则「有没有人在猜」就看不见了）
    assert "[plan]" in everything
    assert "served" in everything
    assert "denied" in everything
