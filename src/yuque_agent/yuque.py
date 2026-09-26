"""语雀 OpenAPI 薄封装（``/api/v2/*``，``X-Auth-Token``）。

自己写一份而不是复用旧项目，是为了让这一层的**能力边界**本身可见：
读取方法随便调，写入方法全部经过 :meth:`YuqueClient._write`（dry-run 与审计的唯一入口），
所以「agent 到底能不能改语雀」在代码里是**一眼可数**的：写方法共 7 个，
其中 ``toc_rename`` **没有注册给任何工具集**（见 `docs/design.md` D9）。

实测要点（踩过的坑）：

* 文档只能用 **slug** 取（``GET /docs/{doc_id}`` 是 404）；
* 文档列表 ``limit`` 上限 **100**，超了 422；
* 目录写操作的入口是 ``PUT /api/v2/repos/{repo}/toc``，靠 ``action`` 区分：

  | 目的 | payload |
  |---|---|
  | 新建分组标题到某父节点末尾 | ``{action:appendNode, action_mode:child, type:TITLE, title, target_uuid?}`` |
  | 移动节点到某父节点末尾 | ``{action:appendNode, action_mode:child, node_uuid, target_uuid?}`` |
  | 从目录移除节点 | ``{action:removeNode, action_mode:sibling, node_uuid}`` |

* 目录读写之间有**秒级延迟**，写完立刻读可能看不到新节点（不要据此重试）。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

import httpx

from .config import PAGE_SIZE, USER_AGENT


class YuqueError(RuntimeError):
    """语雀 API 返回的错误（已带上人类可读的 message）。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 模型


@dataclass(frozen=True)
class TocNode:
    uuid: str
    type: str
    """``DOC`` / ``TITLE`` / ``LINK``"""
    title: str
    doc_id: int
    slug: str
    parent_uuid: str
    depth: int
    """根节点为 1。"""
    path: str
    """人类可读位置，如 ``归档区/0912-0918``。"""
    order: int
    """在目录里的先后次序（0 起）。"""


@dataclass(frozen=True)
class DocMeta:
    doc_id: int
    slug: str
    title: str
    updated_at: str
    created_at: str
    author: str
    author_login: str
    word_count: int
    doc_type: str = "Doc"


@dataclass(frozen=True)
class DocDetail(DocMeta):
    body: str = ""
    """markdown 正文。"""


# ---------------------------------------------------------------- 客户端


class YuqueClient:
    """语雀客户端。``dry_run=True`` 时所有写操作只记录不执行。"""

    def __init__(
        self,
        *,
        host: str,
        token: str,
        repo: str,
        dry_run: bool = False,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise YuqueError("缺少语雀 token")
        self.host = host.rstrip("/")
        self.repo = repo
        self.dry_run = dry_run
        self.scopes = ""
        self._slug_cache: dict[int, str] = {}
        self._client = httpx.Client(
            base_url=self.host,
            headers={"X-Auth-Token": token, "User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> YuqueClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 底层 -------------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        try:
            resp = self._client.request(
                method,
                path,
                params={k: v for k, v in (params or {}).items() if v is not None},
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise YuqueError(f"网络请求失败（{type(exc).__name__}）：{exc}") from exc
        scopes = resp.headers.get("x-oauth-scopes")
        if scopes:
            self.scopes = scopes
        if resp.status_code >= 400:
            message = resp.text[:300]
            try:
                payload = resp.json()
                message = str(payload.get("message") or payload.get("error") or message)
            except ValueError:
                pass
            raise YuqueError(message, status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise YuqueError("语雀返回了非 JSON 响应") from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _get(self, path: str, **params: Any) -> Any:
        return self._unwrap(self._request("GET", path, params=params))

    def _write(self, method: str, path: str, *, op: str, json_body: Any) -> Any:
        """所有写操作的唯一入口——dry-run 与审计都收在这里。

        dry-run 的留痕在 `RunContext.note_kb_write()`（会进 session 与 result.json），
        这里只负责「不真的发请求」。
        """
        if self.dry_run:
            return {"__dry_run__": True, "op": op, "path": path}
        return self._unwrap(self._request(method, path, json_body=json_body))

    # -- 读 ---------------------------------------------------------------
    def repo_info(self) -> dict[str, Any]:
        data = self._get(f"/api/v2/repos/{self.repo}")
        return data if isinstance(data, dict) else {}

    def toc(self) -> list[TocNode]:
        raw = self._get(f"/api/v2/repos/{self.repo}/toc") or []
        nodes: dict[str, TocNode] = {}
        for index, item in enumerate(raw):
            doc_id = _to_int(item.get("doc_id"))
            slug = str(item.get("slug") or "")
            nodes[str(item.get("uuid") or "")] = TocNode(
                uuid=str(item.get("uuid") or ""),
                type=str(item.get("type") or ""),
                title=str(item.get("title") or ""),
                doc_id=doc_id,
                slug="" if slug == "#" else slug,
                parent_uuid=str(item.get("parent_uuid") or ""),
                depth=0,
                path="",
                order=index,
            )
        # 补 depth / path（API 的 depth 只有层级数，不好直接用来拼路径）
        resolved: dict[str, TocNode] = {}

        def resolve(uuid: str) -> TocNode:
            if uuid in resolved:
                return resolved[uuid]
            node = nodes[uuid]
            if node.parent_uuid and node.parent_uuid in nodes:
                parent = resolve(node.parent_uuid)
                node = replace(node, depth=parent.depth + 1, path=f"{parent.path}/{node.title}")
            else:
                node = replace(node, depth=1, path=node.title)
            resolved[uuid] = node
            return node

        out = [resolve(uuid) for uuid in nodes]
        out.sort(key=lambda n: n.order)
        return out

    def docs(self) -> list[DocMeta]:
        out: list[DocMeta] = []
        offset = 0
        while True:
            page = self._get(f"/api/v2/repos/{self.repo}/docs", limit=PAGE_SIZE, offset=offset)
            page = page or []
            for item in page:
                meta = _to_doc_meta(item)
                self._slug_cache[meta.doc_id] = meta.slug
                out.append(meta)
            if len(page) < PAGE_SIZE:
                break
            offset += len(page)
        return out

    def resolve_slug(self, ref: int | str) -> str:
        """把 doc_id 或 slug 统一成 slug（语雀不允许按 id 取文档）。"""
        text = str(ref).strip()
        if not text:
            raise YuqueError("文档标识不能为空")
        if text.isdigit():
            doc_id = int(text)
            if doc_id not in self._slug_cache:
                self.docs()  # 填充缓存
            if doc_id not in self._slug_cache:
                raise YuqueError(f"知识库中找不到 doc_id={doc_id} 的文档")
            return self._slug_cache[doc_id]
        return text

    def doc(self, ref: int | str) -> DocDetail:
        slug = self.resolve_slug(ref)
        data = self._get(f"/api/v2/repos/{self.repo}/docs/{slug}", raw=1)
        if not isinstance(data, dict):
            raise YuqueError(f"读取文档失败：{slug}")
        meta = _to_doc_meta(data)
        return DocDetail(**meta.__dict__, body=str(data.get("body") or ""))

    # -- 写（只有这 5 个）--------------------------------------------------
    def create_doc(
        self, *, title: str, body: str, slug: str = "", public: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body, "format": "markdown"}
        if slug:
            payload["slug"] = slug
        if public is not None:
            payload["public"] = public
        return self._write(
            "POST", f"/api/v2/repos/{self.repo}/docs", op="create_doc", json_body=payload
        )

    def update_doc(
        self, doc_id: int | str, *, title: str | None = None, body: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"format": "markdown"}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        return self._write(
            "PUT", f"/api/v2/repos/{self.repo}/docs/{doc_id}", op="update_doc", json_body=payload
        )

    def delete_doc(self, doc_id: int | str) -> dict[str, Any]:
        return self._write(
            "DELETE", f"/api/v2/repos/{self.repo}/docs/{doc_id}", op="delete_doc", json_body=None
        )

    def toc_add(
        self,
        *,
        title: str = "",
        doc_ids: list[int] | None = None,
        target_uuid: str = "",
        prepend: bool = False,
    ) -> Any:
        """往目录里加节点。

        * 只给 ``title`` → 新建一个分组标题（TITLE）；
        * 只给 ``doc_ids`` → 把已有文档挂进目录。
        """
        if not title and not doc_ids:
            raise YuqueError("toc_add 需要 title 或 doc_ids")
        payload: dict[str, Any] = {
            "action": "prependNode" if prepend else "appendNode",
            "action_mode": "child",
            "type": "TITLE" if title and not doc_ids else "DOC",
        }
        if title:
            payload["title"] = title
        if doc_ids:
            payload["doc_ids"] = doc_ids
        if target_uuid:
            payload["target_uuid"] = target_uuid
        return self._write("PUT", f"/api/v2/repos/{self.repo}/toc", op="toc_add", json_body=payload)

    def toc_move(self, *, node_uuid: str, target_uuid: str = "", prepend: bool = False) -> Any:
        """把节点移到 ``target_uuid`` 下：默认落在**末尾**，``prepend=True`` 落在**最前**；
        不带 ``target_uuid`` 就是根目录。

        ``prepend`` 是 2026-09-26 实测过的（``action=prependNode`` + ``node_uuid`` 真的会把节点
        挪到最前面）。没有它，「归档区内部最新在最上」就只能把整列节点倒着重排一遍。
        """
        payload: dict[str, Any] = {
            "action": "prependNode" if prepend else "appendNode",
            "action_mode": "child",
            "node_uuid": node_uuid,
        }
        if target_uuid:
            payload["target_uuid"] = target_uuid
        return self._write(
            "PUT", f"/api/v2/repos/{self.repo}/toc", op="toc_move", json_body=payload
        )

    def toc_remove(self, *, node_uuid: str, with_children: bool = False) -> Any:
        payload: dict[str, Any] = {
            "action": "removeNode",
            "action_mode": "child" if with_children else "sibling",
            "node_uuid": node_uuid,
        }
        return self._write(
            "PUT", f"/api/v2/repos/{self.repo}/toc", op="toc_remove", json_body=payload
        )

    def toc_rename(self, *, node_uuid: str, title: str) -> Any:
        """改目录节点标题（实测：``editNode`` + ``node_uuid`` + ``title`` 有效）。"""
        payload: dict[str, Any] = {
            "action": "editNode",
            "action_mode": "sibling",
            "node_uuid": node_uuid,
            "title": title,
        }
        return self._write(
            "PUT", f"/api/v2/repos/{self.repo}/toc", op="toc_rename", json_body=payload
        )

    # -- 便捷组合 ---------------------------------------------------------
    def wait_toc_settled(self, seconds: float = 3.0) -> None:
        """语雀目录有写后延迟；需要「写完立刻读」时显式等一下。"""
        time.sleep(seconds)


# ---------------------------------------------------------------- 解析助手


def doc_dir_map(nodes: list[TocNode]) -> dict[int, str]:
    """``doc_id -> 它所在的目录路径``（根目录下的文档算 ``""``）。

    不能直接用节点自身的 ``path``——对 DOC 节点而言那是「父目录/文档名」，
    而我们要的是**容纳它的目录**；也不能去切 ``path`` 字符串
    （文档标题里可以带 ``/``，实测踩到过）。

    算法只有 :func:`_dir_map_by_parent` 这一份，``tools`` 走
    :func:`dir_map_from_payload`（同一份实现的载荷版）。
    """
    return _dir_map_by_parent((n.uuid, n.doc_id, n.parent_uuid, n.path) for n in nodes)


def dir_map_from_payload(toc: list[dict[str, Any]]) -> dict[int, str]:
    """:func:`doc_dir_map` 的载荷版：``toc`` 是变更报告里那份 list[dict]。"""
    return _dir_map_by_parent(
        (
            str(item.get("uuid") or ""),
            _to_int(item.get("doc_id")),
            str(item.get("parent_uuid") or ""),
            str(item.get("path") or ""),
        )
        for item in toc
    )


def _dir_map_by_parent(rows: Iterable[tuple[str, int, str, str]]) -> dict[int, str]:
    """``(uuid, doc_id, parent_uuid, path)`` 四元组 → ``doc_id -> 父目录 path``。"""
    rows = list(rows)
    path_of = {uuid: path for uuid, _, _, path in rows}
    return {doc_id: path_of.get(parent_uuid, "") for _, doc_id, parent_uuid, _ in rows if doc_id}


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_doc_meta(item: dict[str, Any]) -> DocMeta:
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    author = str((creator or {}).get("name") or (user or {}).get("name") or "")
    author_login = str((creator or {}).get("login") or (user or {}).get("login") or "")
    return DocMeta(
        doc_id=_to_int(item.get("id")),
        slug=str(item.get("slug") or ""),
        title=str(item.get("title") or ""),
        updated_at=str(item.get("updated_at") or ""),
        created_at=str(item.get("created_at") or ""),
        author=author,
        author_login=author_login,
        word_count=_to_int(item.get("word_count")),
        doc_type=str(item.get("type") or "Doc"),
    )
