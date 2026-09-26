"""真实 `YuqueClient` 的解析与请求构造（不再靠 FakeYuque 间接覆盖）。

为什么单独测：`yuque.py` 是本项目唯一直接跟语雀说话的层，而它以前**一行测试都没有**——
所有测试都走 `tests/fakes.FakeYuque`。假件是「我们以为语雀长什么样」，真件里的
depth/path 递归、`#` slug、分页循环、错误映射一旦写错，假件不会告诉你
（`docs/test-report.md` 的教训 1 就是「假件太善良」）。这一组补齐。
"""

from __future__ import annotations

import pytest

from yuque_agent import yuque
from yuque_agent.yuque import YuqueClient, YuqueError


class _Resp:
    def __init__(
        self,
        status_code: int = 200,
        payload: object | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self) -> object:
        if self._payload is None:
            raise ValueError("不是 JSON")
        return self._payload


class _HTTP:
    """替掉 ``YuqueClient._client``：记下请求，按脚本回。"""

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def request(self, method: str, path: str, **kwargs: object) -> _Resp:
        self.calls.append({"method": method, "path": path, **kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, _Resp)
        return item

    def close(self) -> None:
        pass


def client(monkeypatch: pytest.MonkeyPatch, http: _HTTP, *, dry_run: bool = False) -> YuqueClient:
    instance = YuqueClient(host="https://www.yuque.com", token="t", repo="g/kb", dry_run=dry_run)
    monkeypatch.setattr(instance, "_client", http)
    return instance


# ---------------------------------------------------------------- 目录树解析


def test_toc_builds_depth_path_and_doc_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(
        _Resp(
            payload={
                "data": [
                    {"uuid": "a", "type": "TITLE", "title": "归档区", "parent_uuid": ""},
                    {"uuid": "b", "type": "TITLE", "title": "0912-0918", "parent_uuid": "a"},
                    {
                        "uuid": "c",
                        "type": "DOC",
                        "title": "见面会",
                        "doc_id": 7,
                        "parent_uuid": "b",
                        "slug": "s7",
                    },
                ]
            }
        )
    )

    nodes = client(monkeypatch, http).toc()

    assert [n.title for n in nodes] == ["归档区", "0912-0918", "见面会"]
    assert [n.depth for n in nodes] == [1, 2, 3]
    assert [n.path for n in nodes] == ["归档区", "归档区/0912-0918", "归档区/0912-0918/见面会"]
    assert [n.order for n in nodes] == [0, 1, 2]
    assert nodes[2].doc_id == 7


def test_toc_tolerates_orphan_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """父节点不在返回里（被删了一半）也不能炸——当成根节点，path 用自己的标题。"""
    http = _HTTP(
        _Resp(
            payload={
                "data": [{"uuid": "x", "type": "TITLE", "title": "孤儿子", "parent_uuid": "?"}]
            }
        )
    )
    nodes = client(monkeypatch, http).toc()
    assert nodes[0].depth == 1 and nodes[0].path == "孤儿子"


# ---------------------------------------------------------------- 文档列表与 slug


def _meta(doc_id: int) -> dict:
    return {
        "id": doc_id,
        "slug": f"s{doc_id}",
        "title": f"文档{doc_id}",
        "updated_at": "t",
        "created_at": "c",
        "creator": {"name": "张三"},
    }


def test_docs_paginates_until_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(yuque, "PAGE_SIZE", 2)
    http = _HTTP(
        _Resp(payload={"data": [_meta(1), _meta(2)]}),
        _Resp(payload={"data": [_meta(3)]}),
    )

    metas = client(monkeypatch, http).docs()

    assert [m.doc_id for m in metas] == [1, 2, 3]
    assert [call["params"]["offset"] for call in http.calls] == [0, 2]
    assert metas[0].author == "张三"


def test_resolve_slug_by_id_uses_the_docs_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(_Resp(payload={"data": [_meta(7)]}), _Resp(payload={"data": [_meta(7)]}))
    instance = client(monkeypatch, http)

    assert instance.resolve_slug(7) == "s7"
    assert instance.resolve_slug("s8") == "s8", "给了 slug 就原样用"
    with pytest.raises(YuqueError):
        instance.resolve_slug(999)
    with pytest.raises(YuqueError):
        instance.resolve_slug("")


def test_doc_is_read_by_slug_with_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """实测坑 P1：语雀只认 slug（按 doc_id 取是 404），所以必须先换成 slug。"""
    http = _HTTP(
        _Resp(payload={"data": [_meta(7)]}),
        _Resp(payload={"data": {**_meta(7), "body": "正文"}}),
    )

    detail = client(monkeypatch, http).doc(7)

    assert detail.body == "正文"
    assert http.calls[-1]["path"].endswith("/docs/s7")
    assert http.calls[-1]["params"]["raw"] == 1


# ---------------------------------------------------------------- 请求与错误


def test_api_error_message_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(_Resp(status_code=404, payload={"message": "文档不存在"}, text="Not Found"))
    with pytest.raises(YuqueError) as exc:
        client(monkeypatch, http).repo_info()
    assert "文档不存在" in str(exc.value)
    assert exc.value.status == 404


def test_non_json_response_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(_Resp(text="<html>502</html>"))
    with pytest.raises(YuqueError):
        client(monkeypatch, http).repo_info()


def test_network_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    http = _HTTP(httpx.ConnectError("dns 挂了"))
    with pytest.raises(YuqueError) as exc:
        client(monkeypatch, http).repo_info()
    assert "网络请求失败" in str(exc.value)


def test_scopes_header_is_captured(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(_Resp(payload={"data": {}}, headers={"x-oauth-scopes": "repo,doc"}))
    instance = client(monkeypatch, http)
    instance.repo_info()
    assert instance.scopes == "repo,doc"


# ---------------------------------------------------------------- 写操作


def test_dry_run_write_never_hits_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP()
    instance = client(monkeypatch, http, dry_run=True)

    result = instance.toc_move(node_uuid="u1", target_uuid="u2", prepend=True)

    assert result["__dry_run__"] is True
    assert http.calls == []


def test_toc_move_prepend_uses_the_prepend_action(monkeypatch: pytest.MonkeyPatch) -> None:
    http = _HTTP(_Resp(payload={"data": {}}))
    client(monkeypatch, http).toc_move(node_uuid="u1", prepend=True)

    sent = http.calls[0]
    assert sent["method"] == "PUT" and sent["path"].endswith("/toc")
    assert sent["json"]["action"] == "prependNode"
    assert sent["json"]["node_uuid"] == "u1"
    assert "target_uuid" not in sent["json"], "根目录不该带 target_uuid"


def test_toc_add_without_title_or_docs_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(YuqueError):
        client(monkeypatch, _HTTP()).toc_add()
