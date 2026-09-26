"""把一次 run 的 session 渲染成人话（``yqa render`` 用）。

**只渲染，不落盘、不写语雀。** 以前这里还有「渲染后写回语雀《工作日志》」那一半，
2026-09-26 之后没有了：《工作日志》被《Agent 通知》（程序维护，见 :mod:`.noticedoc`）
和 8787 端口的 ``/log`` 页取代。

渲染的原则是**不加工**：思考原样贴、工具参数与结果原样贴、错误原样贴。
渲染只负责排版，不负责美化——美化会让证据失真。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .session import read_events

MAX_BLOCK_CHARS = 2500


def render_session(path: Path) -> str:
    """把一个 ``session.jsonl`` 渲染成一节 markdown。"""
    events = list(read_events(path))
    if not events:
        return "_（session 为空）_"

    start = next((e for e in events if e.get("t") == "run_start"), {})
    end = next((e for e in reversed(events) if e.get("t") == "run_end"), {})
    at = str(end.get("at") or start.get("at") or "")
    kind = str(start.get("kind") or "")
    verdict = str(end.get("verdict") or "—")
    summary = str(end.get("summary") or "")

    lines: list[str] = [f"## {at} · {kind} · `{verdict}`", ""]
    if summary:
        lines += [f"**摘要**：{summary}", ""]

    report = next((e for e in events if e.get("t") == "user"), {})
    counts = _counts_from_report(report.get("content", ""))
    if counts:
        lines += [f"**变更报告**：{counts}", ""]

    usage = end.get("usage") or {}
    lines += [
        "| 指标 | 值 |",
        "|---|---|",
        f"| 步数 | {end.get('steps', 0)} |",
        f"| 工具调用 | {end.get('tool_calls', 0)} |",
        f"| token（入/出） | {usage.get('in', 0)} / {usage.get('out', 0)} |",
        f"| 停止原因 | {end.get('stop_reason', '')} |",
        "",
    ]
    if end.get("error"):
        lines += [f"> ⚠️ **本轮出错**：{end['error']}", ""]

    lines += ["<details><summary>完整时间线（点击展开）</summary>", ""]
    lines += _render_timeline(events)
    lines += ["</details>", ""]

    return "\n".join(lines)


def _render_timeline(events: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for event in events:
        kind = event.get("t")
        if kind == "assistant":
            step = int(event.get("step") or 0)
            out.append(f"**第 {step} 步 · 模型输出**")
            if event.get("reasoning"):
                out += ["", "🧠 **思考**", "", _quote(event["reasoning"]), ""]
            if event.get("content"):
                out += ["💬 **正文**", "", _quote(event["content"]), ""]
            calls = event.get("tool_calls") or []
            if calls:
                names = "、".join(f"`{c.get('name')}`" for c in calls)
                out += [f"→ 调用工具：{names}", ""]
        elif kind == "tool":
            name = event.get("name")
            mark = "✅" if event.get("ok") else "❌"
            out += [f"{mark} **工具 `{name}`**（{event.get('ms', 0)} ms）", ""]
            out += [
                "```json",
                _block(json.dumps(event.get("args") or {}, ensure_ascii=False)),
                "```",
                "",
            ]
            body = event.get("result")
            out += ["```json", _block(json.dumps(body, ensure_ascii=False)), "```", ""]
        elif kind == "error":
            out += [f"> ⚠️ {event.get('message')}", ""]
    return out


def _quote(text: str) -> str:
    return "\n".join("> " + line if line.strip() else ">" for line in _block(text).splitlines())


def _block(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_BLOCK_CHARS:
        return text
    return text[:MAX_BLOCK_CHARS] + f"\n…（已截断，全文 {len(text)} 字）"


def _counts_from_report(report_text: str) -> str:
    try:
        payload = json.loads(report_text)
    except (ValueError, TypeError):
        return ""
    counts = payload.get("counts") or {}
    toc = "、目录结构变化" if payload.get("toc_changed") else ""
    return (
        f"新增 {counts.get('added', 0)} / 修改 {counts.get('updated', 0)} / "
        f"删除 {counts.get('removed', 0)}{toc}"
    )
