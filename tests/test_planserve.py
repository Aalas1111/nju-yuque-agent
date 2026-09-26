"""``plan.json`` 下载口（**无密钥，打开即下载**）。

它现在是**公开**的 HTTP 端点，所以测试的重点从「守住密钥」变成了
「**只可能发出那几个文件**」：

* 路径不可越狱（URL 里的东西从不参与拼路径）；
* 旁边的 `plan.defaults.json`（借用人**姓名 + 手机号**）永远取不到；
* 拿到的是原始字节、且不可缓存（缓存了 cac 会提交上周那份）。

`defaults` 是唯一的 PII 面：它被内联进 `plan.json`，所以一旦有人填了真名/手机号，
就会跟着公开 —— 所以专门测一句「非空时要告警」。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from yuque_agent import planserve
from yuque_agent.config import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    s.plan_file.parent.mkdir(parents=True, exist_ok=True)
    s.plan_file.write_text(
        json.dumps(
            {
                "cycle": "0919-0925",
                "generated_at": "2026-09-20T10:00:00+08:00",
                "defaults": {},
                "activities": [{"date": "2026-09-23", "period": "7-8", "title": "新生见面会"}],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return s


@pytest.fixture()
def server(settings: Settings):
    settings.plan_port = 0  # 让内核挑一个空闲端口
    httpd = planserve.build_server(settings, host="127.0.0.1")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def get(url: str) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


# -- 打开即下载 -----------------------------------------------------------


def test_download_needs_no_key(settings: Settings, server: str) -> None:
    """cac 的要求：访问 download 立刻下载。"""
    status, body, headers = get(f"{server}/download")
    assert status == 200
    assert json.loads(body)["cycle"] == "0919-0925"
    assert "attachment" in headers.get("Content-Disposition", "")
    assert 'filename="plan.json"' in headers.get("Content-Disposition", "")


def test_plan_json_is_an_alias_for_scripts(settings: Settings, server: str) -> None:
    """同一条路径，方便 curl / 脚本直接用。"""
    assert get(f"{server}/plan.json") == get(f"{server}/download")


def test_served_bytes_are_identical_to_the_file(settings: Settings, server: str) -> None:
    """必须是**原字节**——中间做任何 JSON 往返都可能改变下游读到的东西。"""
    status, body, _ = get(f"{server}/download")
    assert status == 200
    assert body == settings.plan_file.read_bytes()


def test_download_is_never_cached(settings: Settings, server: str) -> None:
    """cac 拿到的必须是**此刻**那份。

    一旦中间缓存了，他会提交一个上周的清单 —— 而这里谁都看不出来。
    """
    _, _, headers = get(f"{server}/download")
    assert "no-store" in headers.get("Cache-Control", "")


def test_landing_page_shows_which_cycle_you_are_looking_at(settings: Settings, server: str) -> None:
    """人从网页进来时，得一眼看出「这是哪一周、几条」。"""
    status, body, headers = get(f"{server}/")
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    text = body.decode("utf-8")
    assert "0919-0925" in text
    assert "新生见面会" in text
    assert "/download" in text


def test_landing_page_says_so_when_there_is_no_plan(settings: Settings, server: str) -> None:
    settings.plan_file.unlink()
    status, body, _ = get(f"{server}/")
    assert status == 200
    assert "还没有清单" in body.decode("utf-8")


def test_healthz_is_contentless(server: str) -> None:
    status, body, _ = get(f"{server}/healthz")
    assert status == 200 and body.strip() == b"ok"


# -- 只可能发出那几个文件（现在是公开端点，这些更要紧）--------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../../../etc/passwd",
        "/download/../../../../etc/passwd",
        "/plan.json/../../../../etc/passwd",
        "/outbox/plan.defaults.json",
        "/plan.defaults.json",
        "/../../plan.defaults.json",
        "/archive/../../plan.json",
        "/archive/..%2f..%2fplan.json",
        "/archive/0919-0925/../../../plan.json",
        "/archive/0919-0925/applications/index.json",
        "/applications/index.json",
        "/notes",
    ],
)
def test_only_the_allowlisted_files_are_reachable(server: str, path: str) -> None:
    """路径不可越狱。**公开端点下这条是主要防线。**"""
    status, body, _ = get(f"{server}{path}")
    assert status == 404, f"{path} 竟然返回 {status}"


def test_borrower_pii_file_is_never_reachable(settings: Settings, server: str) -> None:
    """`plan.defaults.json` 里是借用人**姓名与手机号** —— 就在隔壁，但绝不能发。"""
    settings.plan_defaults_file.write_text(
        json.dumps({"JYRXM": "张三", "JYRDH": "13800000000"}, ensure_ascii=False),
        encoding="utf-8",
    )
    for path in ("/plan.defaults.json", "/download/../plan.defaults.json", "/defaults"):
        status, body, _ = get(f"{server}{path}")
        assert status == 404, f"{path} 返回了 {status}"
        assert "13800000000" not in body.decode("utf-8", "replace")


def test_malformed_cycle_is_404(server: str) -> None:
    assert get(f"{server}/archive/not-a-cycle/plan.json")[0] == 404


def test_archive_cycle_is_served_when_it_exists(settings: Settings, server: str) -> None:
    dest = settings.cycle_archive_dir("0912-0918")
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "plan.json").write_text('{"cycle": "0912-0918"}', encoding="utf-8")
    status, body, _ = get(f"{server}/archive/0912-0918/plan.json")
    assert status == 200
    assert json.loads(body)["cycle"] == "0912-0918"


def test_missing_plan_explains_itself(settings: Settings, server: str) -> None:
    """404 要说清是「这一周期还没申请」，而不是让人以为服务坏了。"""
    settings.plan_file.unlink()
    status, body, _ = get(f"{server}/download")
    assert status == 404
    assert "还没有清单" in body.decode("utf-8")


# -- PII：唯一的真风险面 --------------------------------------------------


def test_no_pii_warning_when_defaults_are_empty(settings: Settings) -> None:
    """今天就是这种情况：只暴露活动信息（项目负责人明确接受了）。"""
    assert planserve.pii_warning(settings) == ""


def test_pii_warning_fires_when_defaults_are_filled(settings: Settings) -> None:
    """一旦有人填了借用人信息，它会跟着 plan.json 一起公开 —— 必须说出来。

    这条守卫的意义：**不是阻止**（cac 可能需要这些字段），而是让「公开 PII」
    这件事在启动时就有人知道，而不是哪天被人偶然发现。
    """
    settings.plan_defaults_file.write_text(
        json.dumps({"JYRXM": "张三", "JYRDH": "13800000000"}, ensure_ascii=False),
        encoding="utf-8",
    )
    warning = planserve.pii_warning(settings)
    assert warning
    assert "公开" in warning
    assert "JYRXM" in warning and "JYRDH" in warning


def test_defaults_are_inlined_into_the_plan_and_therefore_public(settings: Settings) -> None:
    """把「为什么 defaults 是 PII 面」钉成事实，免得有人以为它不会被发出去。"""
    from yuque_agent import outputs

    settings.plan_defaults_file.write_text(
        json.dumps({"JYRXM": "张三", "JYRDH": "13800000000"}, ensure_ascii=False),
        encoding="utf-8",
    )
    outputs.publish_plan(settings)
    body = settings.plan_file.read_text(encoding="utf-8")
    assert "13800000000" in body, "defaults 没被内联进 plan.json？那 PII 告警就没意义了"


# -- 日志看得见（但也就只剩这些了）-----------------------------------------


def test_access_is_logged(settings: Settings, server: str, capsys) -> None:
    get(f"{server}/download")
    get(f"{server}/nope")
    out = capsys.readouterr()
    text = out.out + out.err
    assert "[plan]" in text
    assert "served" in text


# -- /log：处理日志（《工作日志》的替代）-----------------------------------


def write_run(
    settings: Settings,
    run_id: str,
    *,
    kind: str = "polling",
    verdict: str = "accepted",
    summary: str = "受理了一篇",
    error: str = "",
) -> None:
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": kind,
                "verdict": verdict,
                "summary": summary,
                "error": error,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_log_lists_runs_newest_first_and_only_conclusions(settings: Settings, server: str) -> None:
    """说人话：时间 / 类型 / 判定 / 摘要——**没有**工具调用和思考过程。"""
    write_run(settings, "20260926-100000-polling-aaaa", summary="上午那轮")
    write_run(settings, "20260926-202005-polling-bbbb", summary="晚上那轮", kind="archive")

    status, body, headers = get(f"{server}/log")
    text = body.decode("utf-8")

    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert text.index("晚上那轮") < text.index("上午那轮"), "最新的必须在最上面"
    assert "2026-09-26 20:20" in text and "归档" in text
    assert "tool_calls" not in text and "reasoning" not in text


def test_log_escapes_agent_text(settings: Settings, server: str) -> None:
    """这是个**公开**页面：agent 写的摘要里带 HTML 时必须转义，不能被当脚本执行。"""
    write_run(settings, "20260926-100000-polling-aaaa", summary="<script>alert(1)</script>")
    _, body, _ = get(f"{server}/log")
    text = body.decode("utf-8")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_log_is_linked_from_the_download_page_and_tolerates_no_runs(
    settings: Settings, server: str
) -> None:
    _, home, _ = get(f"{server}/")
    assert "/log" in home.decode("utf-8"), "下载页要能点到处理日志"
    _, log, _ = get(f"{server}/log")
    assert "还没有任何处理记录" in log.decode("utf-8")
