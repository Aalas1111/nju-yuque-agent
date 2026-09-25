"""手搓的 agent loop。

刻意保持极简——**没有 LangChain、没有框架**。整个循环只有一件事：

```
LLM 说要用什么工具  →  我们执行  →  把结果塞回去  →  重复，直到它调 done
```

每一轮往返的全部细节（包括 `reasoning_content`）都由 :mod:`.session` 留档。

三个上限（防死循环 / 防上下文爆炸）：

* ``max_steps``：LLM↔tool 往返步数；
* ``max_tool_calls``：工具调用总次数；
* 单个工具结果回灌给 LLM 前会被截断（见 :func:`_bounded`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import LLMClient, Usage, assistant_message, tool_message
from .prompts import PromptLoader
from .session import SessionRecorder, Stopwatch, now_iso
from .tools import RunContext, execute, tool_schemas

MAX_RESULT_CHARS = 6000


@dataclass
class RunResult:
    run_id: str
    kind: str
    verdict: str = ""
    summary: str = ""
    steps: int = 0
    tool_calls: int = 0
    usage: Usage = field(default_factory=Usage)
    session_path: str = ""
    emitted: list[dict[str, Any]] = field(default_factory=list)
    kb_writes: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    stop_reason: str = ""
    journal: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "verdict": self.verdict,
            "summary": self.summary,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": self.usage.to_dict(),
            "session_path": self.session_path,
            "emitted": self.emitted,
            "kb_writes": self.kb_writes,
            "error": self.error,
            "stop_reason": self.stop_reason,
            "journal": self.journal,
        }


def run_agent(
    *,
    llm: LLMClient,
    ctx: RunContext,
    prompt: PromptLoader,
    payload: dict[str, Any],
    session: SessionRecorder,
    max_steps: int = 24,
    max_tool_calls: int = 60,
) -> RunResult:
    """跑一轮。``payload`` 是给 LLM 的「变更报告 / 归档指令」。

    想看「这一轮正在发生什么」就 tail ``session.jsonl``（追加写、逐行 flush）。
    进程内进度回调在 QQ 桥拆出去时一并去掉了——桥只读 session 文件，
    见 `docs/interface.md` §1.2。
    """
    result = RunResult(run_id=ctx.run_id, kind=ctx.kind, session_path=str(session.path))
    system = prompt.load(ctx.kind)
    schemas = tool_schemas(ctx.kind)

    session.event(
        "run_start",
        run_id=ctx.run_id,
        kind=ctx.kind,
        at=now_iso(),
        model=llm.model,
        repo=ctx.settings.repo,
        dry_run=ctx.settings.dry_run,
        tools=[t["function"]["name"] for t in schemas],
    )
    session.event("system", content=system)

    report_text = json.dumps(payload, ensure_ascii=False, indent=2)
    session.event("user", content=report_text)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": report_text},
    ]

    try:
        for step in range(1, max_steps + 1):
            result.steps = step
            watch = Stopwatch()
            response = llm.chat(messages, tools=schemas)
            result.usage.add(response.usage)
            session.event(
                "assistant",
                step=step,
                content=response.content,
                reasoning=response.reasoning,
                tool_calls=[
                    {"id": c.id, "name": c.name, "arguments": c.arguments_raw}
                    for c in response.tool_calls
                ],
                usage=response.usage.to_dict(),
                finish_reason=response.finish_reason,
                ms=watch.ms(),
            )

            if not response.wants_tools:
                # LLM 自己停了，没说 done —— 也算一轮结束，但要记下来
                result.stop_reason = "llm_stopped_without_done"
                break

            messages.append(assistant_message(response, send_reasoning=llm.send_reasoning_back))

            for call in response.tool_calls:
                args = call.arguments()
                if "__parse_error__" in args:
                    payload_result = {"ok": False, "error": args["__parse_error__"]}
                    ms = 0
                else:
                    call_watch = Stopwatch()
                    payload_result = execute(ctx, call.name, args)
                    ms = call_watch.ms()
                result.tool_calls += 1
                session.event(
                    "tool",
                    step=step,
                    call_id=call.id,
                    name=call.name,
                    args=args,
                    ok=bool(payload_result.get("ok")),
                    result=payload_result,
                    ms=ms,
                )
                messages.append(tool_message(call.id, _bounded(payload_result)))

            if ctx.finished:
                result.stop_reason = "done"
                break
            if result.tool_calls >= max_tool_calls:
                result.stop_reason = "max_tool_calls"
                break
        else:
            result.stop_reason = "max_steps"

    except Exception as exc:  # noqa: BLE001 - 一轮失败必须留痕，不能静默
        result.error = f"{type(exc).__name__}: {exc}"
        result.stop_reason = result.stop_reason or "error"
        session.event("error", message=result.error)
    finally:
        result.verdict = ctx.verdict or result.verdict
        result.summary = ctx.summary or result.summary
        result.emitted = ctx.emitted
        result.kb_writes = ctx.kb_writes
        session.event(
            "run_end",
            at=now_iso(),
            steps=result.steps,
            tool_calls=result.tool_calls,
            verdict=result.verdict,
            summary=result.summary,
            stop_reason=result.stop_reason,
            error=result.error,
            usage=result.usage.to_dict(),
            emitted=result.emitted,
            kb_writes=result.kb_writes,
        )

    return result


def _bounded(value: dict[str, Any]) -> dict[str, Any]:
    """把过大的工具结果截断后再回灌给 LLM，避免上下文爆炸。"""
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return value
    return {
        "ok": value.get("ok"),
        "truncated": True,
        "note": f"结果超过 {MAX_RESULT_CHARS} 字已截断，需要细节请缩小范围重试",
        "preview": text[:MAX_RESULT_CHARS],
    }


def session_path_for(run_dir: Path) -> Path:
    return run_dir / "session.jsonl"
