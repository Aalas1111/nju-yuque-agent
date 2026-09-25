"""离线测试用的假实现。

**测试绝不联网、绝不碰真语雀**——这一点很重要：这个项目里最危险的操作是
「agent 真的去改知识库」，所以任何一条测试都必须能在不联网的前提下跑，
否则「跑测试」会变成「拿生产知识库做实验」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from yuque_agent.llm import LLMResponse, ToolCall, Usage
from yuque_agent.tools import RunContext
from yuque_agent.yuque import DocDetail, DocMeta, TocNode, YuqueError


@dataclass
class FakeYuque:
    """只实现 tools/snapshot 真正用到的那部分接口。"""

    toc_nodes: list[TocNode] = field(default_factory=list)
    doc_metas: list[DocMeta] = field(default_factory=list)
    bodies: dict[int, str] = field(default_factory=dict)
    dry_run: bool = False
    scopes: str = "repo,doc"
    error_on_doc: set[int] = field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    # -- 读 ---------------------------------------------------------------
    def repo_info(self) -> dict[str, Any]:
        return {"name": "测试知识库", "items_count": len(self.doc_metas)}

    def toc(self) -> list[TocNode]:
        return list(self.toc_nodes)

    def docs(self) -> list[DocMeta]:
        return list(self.doc_metas)

    def resolve_slug(self, ref: int | str) -> str:
        text = str(ref)
        if text.isdigit():
            for meta in self.doc_metas:
                if meta.doc_id == int(text):
                    return meta.slug
            raise YuqueError(f"找不到 doc_id={text}")
        return text

    def doc(self, ref: int | str) -> DocDetail:
        doc_id = int(ref) if str(ref).isdigit() else 0
        if doc_id in self.error_on_doc:
            raise YuqueError("模拟读取失败")
        meta = next((m for m in self.doc_metas if m.doc_id == doc_id), None)
        if meta is None:
            raise YuqueError(f"找不到 doc_id={doc_id}")
        return DocDetail(**meta.__dict__, body=self.bodies.get(doc_id, ""))

    # -- 写 ---------------------------------------------------------------
    def _record(self, op: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((op, kwargs))
        return {"__dry_run__": True, "op": op}

    def create_doc(self, *, title: str, body: str, slug: str = "", public: int | None = None):
        return self._record("create_doc", title=title, body=body)

    def update_doc(self, doc_id: int | str, *, title: str = "", body: str = ""):
        return self._record("update_doc", doc_id=doc_id, body=body)

    def delete_doc(self, doc_id: int | str):
        return self._record("delete_doc", doc_id=doc_id)

    def toc_add(
        self,
        *,
        title: str = "",
        doc_ids: list[int] | None = None,
        target_uuid: str = "",
        prepend: bool = False,
    ):
        return self._record("toc_add", title=title, doc_ids=doc_ids, target_uuid=target_uuid)

    def toc_move(self, *, node_uuid: str, target_uuid: str = ""):
        return self._record("toc_move", node_uuid=node_uuid, target_uuid=target_uuid)

    def toc_remove(self, *, node_uuid: str, with_children: bool = False):
        return self._record("toc_remove", node_uuid=node_uuid, with_children=with_children)

    def wait_toc_settled(self, seconds: float = 0.0) -> None:
        return None

    def close(self) -> None:
        return None

    # 让假件也能当上下文管理器用（真客户端支持 with）
    def __enter__(self) -> FakeYuque:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def make_toc(*spec: tuple[str, str, int, str]) -> list[TocNode]:
    """``spec`` 每项 = (title, type, doc_id, parent_title 或 "")。"""
    nodes: list[TocNode] = []
    uuid_of: dict[str, str] = {}
    for index, (title, node_type, doc_id, parent_title) in enumerate(spec):
        uuid = f"u{index}"
        uuid_of[title] = uuid
        nodes.append(
            TocNode(
                uuid=uuid,
                type=node_type,
                title=title,
                doc_id=doc_id,
                slug=f"s{doc_id}" if doc_id else "",
                parent_uuid=uuid_of.get(parent_title, ""),
                depth=2 if parent_title else 1,
                path=f"{parent_title}/{title}" if parent_title else title,
                order=index,
            )
        )
    return nodes


def make_meta(doc_id: int, title: str, *, updated_at: str = "2026-09-20T00:00:00Z") -> DocMeta:
    return DocMeta(
        doc_id=doc_id,
        slug=f"s{doc_id}",
        title=title,
        updated_at=updated_at,
        created_at="2026-09-19T00:00:00Z",
        author="模拟社员",
        author_login="member",
        word_count=10,
    )


@dataclass
class FakeLLM:
    """按脚本逐个返回预设回复；脚本用完后返回一句「结束」。"""

    script: list[LLMResponse] = field(default_factory=list)
    model: str = "fake-model"
    send_reasoning_back: bool = True
    seen_messages: list[list[dict[str, Any]]] = field(default_factory=list)
    calls: int = 0

    def chat(self, messages, *, tools=None) -> LLMResponse:  # noqa: ANN001
        self.seen_messages.append(list(messages))
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content="(script exhausted)", usage=Usage(1, 1, 2))

    def close(self) -> None:
        return None


def call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id=f"c-{name}",
                name=name,
                arguments_raw=__import__("json").dumps(args, ensure_ascii=False),
            )
        ],
        usage=Usage(10, 5, 15),
    )


@dataclass
class Ctx:
    """给工具测试用的一站式上下文。"""

    ctx: RunContext

    @classmethod
    def build(cls, settings, kind: str = "polling", **kwargs: Any) -> Ctx:
        client = FakeYuque(**kwargs.pop("client_kwargs", {}))
        run_dir = settings.runs_dir / "test-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return cls(
            RunContext(
                settings=settings,
                client=client,  # type: ignore[arg-type]
                run_id="test-run",
                kind=kind,
                run_dir=run_dir,
                toc=kwargs.pop("toc", []),
                docs=kwargs.pop("docs", {}),
            )
        )


# ---------------------------------------------------------------- run 假的 runner
# （从 qq_fakes 搬来：QQ 桥搬走后，控制队列测试还要用它）


@dataclass
class FakeRunResult:
    kind: str = "polling"
    verdict: str = "nothing_to_do"
    summary: str = "无事可做"
    run_id: str = "20260920-101834-polling-abcd"
    steps: int = 1
    tool_calls: int = 0
    emitted: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "verdict": self.verdict,
            "summary": self.summary,
            "run_id": self.run_id,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "emitted": self.emitted,
            "error": self.error,
        }


class FakeRunner:
    """按脚本返回 run 结果；``poll_results`` 里的 ``None`` 表示「没变化」。"""

    def __init__(
        self,
        *,
        poll_results: list[Any] | None = None,
        archive_result: Any = None,
    ) -> None:
        self.poll_results = list(poll_results or [])
        self.archive_result = archive_result
        self.polls = 0
        self.archives = 0
        self.force_flags: list[bool] = []
        self.debounce_flags: list[bool] = []

    def poll_once(
        self,
        *,
        force: bool = False,
        rescan: bool = False,
        now: Any = None,
        debounce: bool = True,
    ):
        self.polls += 1
        self.force_flags.append(force)
        self.debounce_flags.append(debounce)
        if self.poll_results:
            return self.poll_results.pop(0)
        return None

    def archive_once(self, *, now: Any = None):
        self.archives += 1
        return (
            self.archive_result
            if self.archive_result is not None
            else FakeRunResult(kind="archive")
        )
