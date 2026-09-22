"""`yqa doctor` 的 LLM 真实探测（`LLMClient.ping`）。

为什么值得单独测：上面那行「LLM key」只检查变量**存在**，key 过期 / 打错 /
余额耗尽它都显示 OK。对一台要无人值守跑几个月的机器来说，那是个会**静默失效**
的盲区。这一项是唯一会花 token 的检查，所以既要测「成功」，也要测
「失败时说的到底是哪儿的问题」——「key 错了」和「网断了」是两件事。
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from yuque_agent import cli, llm
from yuque_agent.llm import LLMClient, LLMError, describe_llm_error

SRC = Path(__file__).resolve().parent.parent / "src" / "yuque_agent"


# -- 假 HTTP ---------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._payload


class _HTTP:
    """替掉 ``LLMClient._client``：记下请求，按脚本回。"""

    def __init__(self, *, result: _Resp | None = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs: object) -> _Resp:
        self.calls.append({"url": url, **kwargs})
        if self.exc is not None:
            raise self.exc
        assert self.result is not None
        return self.result

    def close(self) -> None:
        pass


def _client(monkeypatch: pytest.MonkeyPatch, http: _HTTP) -> LLMClient:
    client = LLMClient(base_url="https://api.example.com", api_key="sk-test", model="m")
    monkeypatch.setattr(client, "_client", http)
    return client


_OK_BODY = {
    "choices": [
        {"message": {"content": "", "reasoning_content": "..."}, "finish_reason": "length"}
    ],
    "usage": {"prompt_tokens": 35, "completion_tokens": 5, "total_tokens": 40},
}


# -- ping 本身 -------------------------------------------------------------


def test_ping_success_returns_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(result=_Resp(200, _OK_BODY))
    usage = _client(monkeypatch, http).ping()
    assert usage.total_tokens == 40
    # 打的是 chat/completions，且带了 key
    assert http.calls[0]["url"].endswith("/chat/completions")
    assert http.calls[0]["headers"]["Authorization"] == "Bearer sk-test"


def test_ping_uses_tiny_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测必须便宜——不然就没人敢在诊断里用它。"""
    http = _HTTP(result=_Resp(200, _OK_BODY))
    _client(monkeypatch, http).ping()
    assert http.calls[0]["json"]["max_tokens"] <= 32


@pytest.mark.parametrize("status", [401, 402, 403, 404, 429, 500])
def test_ping_raises_with_status(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    http = _HTTP(result=_Resp(status, text="nope"))
    with pytest.raises(LLMError) as exc:
        _client(monkeypatch, http).ping()
    assert exc.value.status == status


def test_ping_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """探测要快：失败就是失败，别退避重试 3 次让人干等。"""
    http = _HTTP(result=_Resp(500, text="boom"))
    with pytest.raises(LLMError):
        _client(monkeypatch, http).ping()
    assert len(http.calls) == 1


def test_ping_wraps_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(exc=httpx.ConnectError("dns 挂了"))
    with pytest.raises(LLMError) as exc:
        _client(monkeypatch, http).ping()
    assert exc.value.status == 0
    assert "网络不通" in str(exc.value)


# -- 失败翻译 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "want"),
    [
        (401, "key 无效"),
        (402, "余额不足"),
        (403, "权限"),
        (404, "模型名或端点"),
        (429, "限流"),
        (503, "服务端故障"),
    ],
)
def test_describe_llm_error_names_the_real_problem(status: int, want: str) -> None:
    assert want in describe_llm_error(LLMError("x", status=status))


def test_describe_llm_error_passes_through_network_message() -> None:
    """网络问题没有状态码，就照原话说（里面已经带了异常类型）。"""
    assert "网络不通" in describe_llm_error(LLMError("网络不通（ConnectError）：dns 挂了"))


def test_describe_llm_error_says_key_is_fine_on_server_side_5xx() -> None:
    """「对方挂了」和「你 key 错了」必须区分开，否则会让人白换 key。"""
    assert "key 本身没问题" in describe_llm_error(LLMError("x", status=502))


# -- doctor 接线 -----------------------------------------------------------


def _fake_llm_factory(*, raises: LLMError | None = None, total: int = 40):
    class _FakeLLM:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> _FakeLLM:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def ping(self) -> llm.Usage:
            if raises is not None:
                raise raises
            return llm.Usage(prompt_tokens=35, completion_tokens=5, total_tokens=total)

    return _FakeLLM


class _FakeYuque:
    def __init__(self, **kwargs: object) -> None:
        self.scopes = "repo,doc"

    def __enter__(self) -> _FakeYuque:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def repo_info(self) -> dict:
        return {"name": "kb", "items_count": 1}

    def toc(self) -> list:
        return []

    def docs(self) -> list:
        return []


def _run_doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, key: str = "sk-test") -> str:
    # 必须把解析函数也钉住：真跑的话它会 fallback 到开发机上的
    # ~/.pi/agent/auth.json，于是「没有 key」这个前提根本不成立。
    monkeypatch.setattr("yuque_agent.config.resolve_llm_key", lambda *a, **k: key)
    monkeypatch.setenv("YQA_TOKEN", "t")
    monkeypatch.setattr(cli, "YuqueClient", _FakeYuque)
    result = CliRunner().invoke(
        cli.app, ["doctor", "--workspace", str(tmp_path / "ws"), "--repo", "g/kb"]
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_doctor_reports_a_successful_probe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "LLMClient", _fake_llm_factory())
    out = _run_doctor(monkeypatch, tmp_path)
    assert "LLM 可用性" in out
    assert "40 tokens" in out


def test_doctor_reports_why_the_key_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """失败时不能只说「调用失败」——要说清是 key 的问题还是网的问题。"""
    monkeypatch.setattr(cli, "LLMClient", _fake_llm_factory(raises=LLMError("x", status=401)))
    out = _run_doctor(monkeypatch, tmp_path)
    assert "LLM 可用性" in out
    assert "key 无效" in out


def test_doctor_skips_probe_without_a_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """没 key 就不探测——不能花 token，也不能报一个假失败。"""

    def _boom(**kwargs: object) -> None:
        raise AssertionError("没有 key 就不该构造 LLMClient")

    monkeypatch.setattr(cli, "LLMClient", _boom)
    out = _run_doctor(monkeypatch, tmp_path, key="")
    assert "LLM 可用性" in out
    assert "跳过" in out


# -- 守卫：探测不许进常驻循环 ---------------------------------------------


def _ping_callers() -> list[str]:
    """哪些模块里出现了 ``*.ping()`` 调用。"""
    found: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "llm.py":  # ping 自己的定义在这
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "ping"
            ):
                found.append(path.name)
    return found


def test_ping_is_only_called_from_the_doctor_path() -> None:
    """`ping` 会花 token。常驻轮询 / agent 循环里调它就是**每一轮都在烧钱**。

    AST 级守卫：只允许 cli.py（doctor）调它，且必须真的调了——
    否则 doctor 那行「LLM 可用性」就是假的。
    """
    callers = set(_ping_callers())
    assert callers == {"cli.py"}, (
        f"ping() 只该在 cli.py 的 doctor 里出现，实际: {sorted(callers) or '没有'}"
    )


def test_long_running_loop_never_probes() -> None:
    """把话说白：常驻循环那三个模块里一次都不许调。

    （用 AST，不用 ``"ping" in src``——那个会把 ``typing`` 也抳进去。）
    """
    for name in ("runner.py", "agent.py", "watcher.py"):
        for node in ast.walk(ast.parse((SRC / name).read_text(encoding="utf-8"))):
            assert not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "ping"
            ), f"{name} 不该调 ping（那是会花钱的探测）"
