"""控制请求队列：外部进程（QQ 桥）→ 核心常驻进程的**唯一**触发通道。

为什么是文件队列，而不是让请求方自己跑一轮：
``state.json`` 只能有一个写者 —— 两个轮询进程互相覆盖快照是记过事故的
（``AGENTS.md`` §2.2、``docs/deploy.md`` §11）。所以请求方只写
``control/requests/*.json``，由常驻进程消费；跑完把回执写回
``control/done/<同名>.json``，请求方轮询 ``done/`` 拿结果。

三种请求（``kind``）：

* ``once``    —— 立刻跑一轮轮询（等价于 ``yqa once``，但由常驻进程执行）
* ``archive`` —— 立刻跑一次归档（会动知识库结构，桥侧要限权）
* ``apply``   —— 落盘一份借用申请。字段是用户原话里提取的**原始值**，
  校验 / 规范化 / 落盘 / 通知**全部在核心做**（请求方不写产物）。

文件格式见 ``docs/interface.md`` §1.2。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from . import clock, outputs, school
from .config import Settings

LogFn = Callable[[str], None]

REQUEST_KINDS = ("once", "archive", "apply")

DONE_KEEP_DAYS = 7


def new_request_name(kind: str) -> str:
    """请求文件名：时间戳开头（天然按时间排序）+ 随机后缀。"""
    return f"{clock.compact_stamp()}-{kind}-{secrets.token_hex(3)}.json"


def take_requests(settings: Settings) -> list[tuple[Path, dict[str, Any]]]:
    """认领 ``requests/`` 里所有能读出来的请求（按文件名 = 时间顺序）。

    读不出来的（半截 JSON）挪进 ``done/`` 并标错误 —— 不能让一个坏文件
    卡住整个队列。
    """
    out: list[tuple[Path, dict[str, Any]]] = []
    requests_dir = settings.control_requests_dir
    if not requests_dir.exists():
        return out
    for path in sorted(requests_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _move(path, settings.control_done_dir / f"{path.stem}.malformed.json")
            continue
        if not isinstance(data, dict):
            _move(path, settings.control_done_dir / f"{path.stem}.malformed.json")
            continue
        out.append((path, data))
    return out


def finish_request(
    settings: Settings,
    path: Path,
    *,
    ok: bool,
    summary: str = "",
    run_id: str = "",
    error: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    """把处理完的请求挪到 ``done/`` 并附上回执（请求方读这个）。"""
    record = {
        "finished_at": clock.stamp(),
        "ok": bool(ok),
        "summary": summary,
        "run_id": run_id,
        "error": error,
    }
    if extra:
        record.update(extra)
    destination = settings.control_done_dir / path.name
    tmp = destination.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    _move(tmp, destination)
    try:
        path.unlink()
    except OSError:  # pragma: no cover - 请求文件删不掉不影响回执
        pass
    return destination


def prune_done(settings: Settings, *, days: int = DONE_KEEP_DAYS) -> int:
    """清掉 ``done/`` 里超过 ``days`` 天的回执（不给磁盘留垃圾）。"""
    done_dir = settings.control_done_dir
    if not done_dir.exists():
        return 0
    cutoff = clock.now().timestamp() - days * 86400
    removed = 0
    for path in done_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover - 清垃圾失败不该影响主流程
            continue
    return removed


def process_pending(
    settings: Settings,
    *,
    runner: Any = None,
    log: LogFn | None = None,
) -> list[dict[str, Any]]:
    """常驻进程每轮调用一次：消费 ``control/requests/``。

    ``runner`` 为 ``None`` 时只处理 ``apply``（不需要跑 LLM 的那种）。
    """
    log = log or (lambda _text: None)
    results: list[dict[str, Any]] = []
    prune_done(settings)
    for path, request in take_requests(settings):
        kind = str(request.get("kind") or "")
        requested_by = str(request.get("requested_by") or "")
        target = request.get("target") if isinstance(request.get("target"), dict) else None

        ok, summary, run_id, error = True, "", "", ""
        if kind in ("once", "archive"):
            if runner is None:
                ok, error = False, "核心进程没有 runner，无法执行轮询/归档"
            else:
                ok, summary, run_id, error = _run_once(runner, kind)
        elif kind == "apply":
            outcome = apply_from_raw(
                settings,
                request.get("raw") or {},
                target=target,
                requested_by=requested_by,
                log=log,
            )
            ok = bool(outcome.get("ok"))
            summary = outcome.get("summary", "")
            error = "; ".join(outcome.get("errors") or [])
        else:
            ok, error = False, f"未知 kind：{kind!r}（支持 {list(REQUEST_KINDS)}）"

        finish_request(settings, path, ok=ok, summary=summary, run_id=run_id, error=error)
        log(f"[control] {path.name} → {'ok' if ok else 'fail'}：{summary or error}")
        results.append(
            {"kind": kind, "ok": ok, "summary": summary, "run_id": run_id, "error": error}
        )
    return results


def _run_once(runner: Any, kind: str) -> tuple[bool, str, str, str]:
    """跑一轮（``once`` / ``archive``），返回 (ok, summary, run_id, error)。"""
    try:
        if kind == "archive":
            result = runner.archive_once()
        else:
            # debounce=False：人工命令（管理员在 QQ 里敲的），敲了就该立刻有结果，
            # 不该被静默期吃掉——和 `yqa once` 同理。
            result = runner.poll_once(force=True, debounce=False)
    except Exception as exc:  # noqa: BLE001 - 失败要回执，不能让队列卡住
        return False, "", "", f"{type(exc).__name__}: {exc}"
    if result is None:
        skip = str(getattr(runner, "last_skip", "") or "")
        reason = {"no_change": "没有变化", "quiet_period": "还在静默期内"}.get(skip) or skip
        reason = reason or "没有变化"
        return True, f"这一轮没有唤醒 LLM（{reason}，0 token）。", "", ""
    return True, _summarize(result), str(getattr(result, "run_id", "")), ""


def _summarize(result: Any) -> str:
    usage = getattr(result, "usage", None)
    tokens = f"{usage.prompt_tokens}/{usage.completion_tokens}" if usage is not None else "?"
    bits = [
        f"kind={getattr(result, 'kind', '')}",
        f"verdict={getattr(result, 'verdict', '') or '—'}",
        f"tokens={tokens}",
    ]
    head = getattr(result, "summary", "") or "(无摘要)"
    return f"{head}\n   " + " · ".join(bits)


# ---------------------------------------------------------------- 申请落盘


def apply_from_raw(
    settings: Settings,
    raw: dict[str, Any],
    *,
    target: dict[str, Any] | None = None,
    requested_by: str = "",
    log: LogFn | None = None,
) -> dict[str, Any]:
    """把「原始字段」的借用申请落盘（**校验与产物写入都在核心**，零 LLM）。

    ``raw``：``activity_name`` / ``date``(YYYY-MM-DD) / ``start``(HH:MM) /
    ``end``(HH:MM) / ``campus``(校区名或代码) / ``people``(整数，可省；
    **不填 = 0 = 不筛容量**，与《指导文档》和 LLM 那条路一致)。

    失败发 ``rejected``、成功发 ``accepted`` 通知；两者都带 ``target``
    （QQ 桥据此直投给申请人本人）。
    """
    log = log or (lambda _text: None)
    today = clock.today()
    errors: list[str] = []

    activity_name = str(raw.get("activity_name") or "").strip()
    if not activity_name:
        errors.append("活动名称必填")

    day_text = str(raw.get("date") or "").strip()
    day: date | None = None
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        errors.append(f"日期格式不对（{day_text or '(空)'}），要 YYYY-MM-DD")
    if day is not None:
        lo, hi = school.bookable_range(today)
        if day < lo or day > hi:
            errors.append(f"日期 {day} 不在可借范围（{lo} ~ {hi}）")

    start = str(raw.get("start") or "").strip()
    end = str(raw.get("end") or "").strip()
    span: tuple[int, int] | None = None
    if school.parse_time(start) is None or school.parse_time(end) is None:
        errors.append(f"时间要 HH:MM（收到 {start or '(空)'} ~ {end or '(空)'}）")
    else:
        span = school.periods_for(start, end)
        if span is None:
            errors.append(f"{start}-{end} 对不上任何节次（可借 08:00-22:20，12:00-14:00 午休）")

    campus_text = str(raw.get("campus") or "").strip()
    campus_code = school.normalize_campus(campus_text)
    if campus_code is None:
        errors.append(f"认不出校区「{campus_text or '(空)'}」（鼓楼/浦口/仙林/苏州）")

    people = raw.get("people")
    if isinstance(people, str) and people.strip().isdigit():
        people = int(people.strip())
    if people is not None and (not isinstance(people, int) or people < 1):
        errors.append(f"人数要是正整数（收到 {people!r}）")
        people = None
    people_source = "user_input" if people is not None else "unspecified"
    if people is None:
        # 不填 = 不限（0 = 不筛容量）：别替社员按 30 计——那会让下游
        # 按 ≥30 人筛教室，小活动被塞进大教室（2026-09-27 负责人拍板）。
        people = 0

    if errors or day is None or span is None or campus_code is None:
        message = "你的借用申请没能受理：\n" + "\n".join(f"- {e}" for e in errors)
        _emit_notice(
            settings,
            kind="rejected",
            summary=f"QQ 申请未受理（{activity_name or '无名称'}）",
            message=message,
            reasons=errors,
            target=target,
            extra={"source": "qq_bot", "requested_by": requested_by},
            log=log,
        )
        return {"ok": False, "errors": errors, "summary": message}

    ksjc, jsjc = span
    payload = {
        "source": {
            "repo": settings.repo,
            "doc_id": _synthetic_doc_id(day.isoformat(), requested_by),
            "title": f"QQ申请-{activity_name}",
            "author": f"qq:{requested_by[:12]}" if requested_by else "qq(未知)",
            "dir": "qq-bot",
            "content_sha256": "",
        },
        "activity": {
            "title": activity_name,
            "date": day.isoformat(),
            "period": school.period_label(ksjc, jsjc),
            "people": people,
            "campus": campus_code,
            "building": None,
            "room_type": None,
            "preferred_room": None,
        },
        "raw": {
            "activity_name": activity_name,
            "date": day.isoformat(),
            "start": start,
            "end": end,
            "campus": campus_text,
            "people": people if people_source == "user_input" else None,
            "source": "qq_bot",
        },
        "derived": {
            "campus_name": school.CAMPUS_CODES.get(campus_code, campus_text),
            "campus_code": campus_code,
            "ksjc": ksjc,
            "jsjc": jsjc,
            "people_source": people_source,
        },
        "agent": {
            "run_id": f"qq-{clock.compact_stamp()}",
            "verdict": "accepted",
            "confidence": "high",
            "notes": ["QQ 桥交互式申请（核心侧校验并落盘）"],
        },
        "normalizations": [],
        "warnings": [],
    }

    try:
        written = outputs.write_application(settings, payload)
    except Exception as exc:  # noqa: BLE001 - 写不进去也要给申请人一个回话
        message = f"申请写入失败：{type(exc).__name__}: {exc}"
        _emit_notice(
            settings,
            kind="rejected",
            summary=f"QQ 申请写入失败（{activity_name}）",
            message=message,
            reasons=[message],
            target=target,
            extra={"source": "qq_bot", "requested_by": requested_by},
            log=log,
        )
        return {"ok": False, "errors": [message], "summary": message}

    application_id = str(written.get("application_id") or "")
    summary = f"申请已受理（{application_id}）"
    message = (
        f"你的教室借用申请已受理。\n"
        f"申请编号：{application_id}\n"
        f"活动：{activity_name}\n"
        f"日期：{day.isoformat()} {start}-{end}\n"
        f"校区：{school.CAMPUS_CODES.get(campus_code, campus_text)}"
    )
    _emit_notice(
        settings,
        kind="accepted",
        summary=summary,
        message=message,
        target=target,
        extra={"source": "qq_bot", "requested_by": requested_by, "application_id": application_id},
        log=log,
    )
    return {"ok": True, "application_id": application_id, "summary": summary, "errors": []}


def _synthetic_doc_id(day: str, who: str) -> int:
    """QQ 申请没有语雀文档，用「日期 + 请求人」合成一个稳定 id。

    稳定 = 同一个人同一天重复提交只会覆盖同一个申请（和语雀侧「一篇文档
    重复受理覆盖同一个文件」的幂等语义一致）。
    """
    digest = hashlib.sha256(f"qq\x00{who}\x00{day}".encode()).hexdigest()
    return int(digest[:8], 16) % (10**9)


def _emit_notice(
    settings: Settings,
    *,
    kind: str,
    summary: str,
    message: str,
    target: dict[str, Any] | None = None,
    reasons: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    log: LogFn | None = None,
) -> None:
    payload: dict[str, Any] = {
        "summary": summary,
        "message": message,
        "reasons": reasons or [],
        "extra": extra or {},
    }
    if target and target.get("scope") and target.get("target_id"):
        payload["target"] = {"scope": str(target["scope"]), "target_id": str(target["target_id"])}
    try:
        outputs.write_notice(settings, kind=kind, payload=payload)
    except Exception as exc:  # noqa: BLE001 - 通知发不出去不能拖垮队列（会记日志）
        if log:
            log(f"[control] 通知写入失败：{type(exc).__name__}: {exc}")


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.replace(destination)
    except OSError:  # pragma: no cover - 跨设备时退回复制
        shutil.move(str(source), str(destination))


__all__ = [
    "REQUEST_KINDS",
    "apply_from_raw",
    "finish_request",
    "new_request_name",
    "process_pending",
    "prune_done",
    "take_requests",
]
