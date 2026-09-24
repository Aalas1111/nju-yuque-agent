"""逐用户的交互式教室借用申请会话（LLM 驱动）。

设计：

* 每个用户（按 sender_id 隔离）同一时刻最多一个活跃会话；
* 每条用户消息都经过 LLM 提取结构化字段（活动名、日期、时间、校区、人数），
  能提多少提多少，然后只问还缺的部分；
* 用户随时可以发 ``/cancel`` 或 ``取消`` 退出；
* 收集完毕后调用 :func:`outputs.write_application` 落盘，
  并写一条通知到 ``pending/``（带 ``direct_target``，直接回给申请人）。

LLM 只负责「从自然语言提取结构化信息」——校验、状态管理、payload 构造
仍然是确定性的，零歧义。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import clock, llm, outputs, school
from ..config import Settings

# ---------------------------------------------------------------- LLM 提取

_SYSTEM_PROMPT = """\
你是一个教室借用申请的信息提取助手只返回JSON不返回任何其他内容。

从用户消息中提取以下字段：
- activity_name: 活动名称（字符串）
- date: 活动日期，格式 YYYY-MM-DD（字符串）
- start_time: 开始时间，格式 HH:MM（字符串）
- end_time: 结束时间，格式 HH:MM（字符串）
- campus: 校区，只能是：鼓楼、浦口、仙林、苏州（字符串）
- people: 预计参加人数（整数）

规则：
1. 今天是 {today}，"明天"={tomorrow}，"后天"={day_after}，"下周一"等请换算成具体日期。
2. 只能提取消息中明确提到或能直接推算的信息，不要编造。
3. 没有提到的字段留空字符串或 null。
4. 只返回 JSON 对象，不要有任何其他文字、markdown 或代码块标记。"""

_USER_PROMPT = "请从以下消息中提取信息：\n\n{user_text}"


def _extract_with_llm(
    client: llm.LLMClient, user_text: str
) -> dict[str, Any]:
    """调 LLM 从用户消息提取结构化字段。失败返回空 dict。"""
    today = date.today()
    system = _SYSTEM_PROMPT.format(
        today=today.isoformat(),
        tomorrow=(today + timedelta(days=1)).isoformat(),
        day_after=(today + timedelta(days=2)).isoformat(),
    )
    try:
        resp = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": _USER_PROMPT.format(user_text=user_text)},
            ],
        )
    except Exception:
        return {}

    text = (resp.content or "").strip()
    # 容忍 LLM 包 ```json ... ```
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(data, dict):
        return {}

    # 清洗：空串 → 删除，类型归一化
    result: dict[str, Any] = {}
    for key in ("activity_name", "date", "start_time", "end_time", "campus"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            result[key] = v.strip()
    people = data.get("people")
    if isinstance(people, int) and people >= 0:
        result["people"] = people
    elif isinstance(people, str) and people.isdigit():
        result["people"] = int(people)
    return result


# ---------------------------------------------------------------- 校验


def _validate_date_str(date_str: str) -> tuple[date | None, str | None]:
    """校验日期字符串并返回 (date_obj, error_msg)。"""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return None, f"日期格式不对（{date_str}），请用 YYYY-MM-DD。"
    today = date.today()
    lo, hi = school.bookable_range(today)
    if d < lo:
        return None, f"日期 {d} 距今天不足 {school.MIN_DAYS_AHEAD} 天（最早 {lo}）"
    if d > hi:
        return None, f"日期 {d} 超出提前 {school.MAX_DAYS_AHEAD} 天上限（最晚 {hi}）"
    return d, None


def _validate_time_pair(
    start: str, end: str
) -> tuple[tuple[int, int] | None, str | None]:
    if not start or not end:
        return None, "请同时提供开始和结束时间（如 14:00-16:00）"
    if school.parse_time(start) is None or school.parse_time(end) is None:
        return None, "时间格式不对，请用 HH:MM（如 14:00 和 16:00）"
    s = school.parse_time(start)
    e = school.parse_time(end)
    if s is not None and e is not None and s >= e:
        return None, "结束时间必须晚于开始时间"
    span = school.periods_for(start, end)
    if span is None:
        return None, (
            f"{start}-{end} 对不上任何节次（可借 08:00-22:20，12:00-14:00 午休）"
        )
    return span, None


# ---------------------------------------------------------------- 辅助


def _summary(answers: dict[str, Any]) -> str:
    lines: list[str] = []
    if answers.get("activity_name"):
        lines.append(f"活动名称：{answers['activity_name']}")
    if answers.get("date"):
        lines.append(f"活动日期：{answers['date']}")
    if answers.get("start") and answers.get("end"):
        lines.append(f"活动时间：{answers['start']} ~ {answers['end']}")
    if answers.get("campus_name"):
        lines.append(f"校区：{answers['campus_name']}")
    if answers.get("people") is not None:
        lines.append(f"预计人数：{answers['people']}")
    return "\n".join(lines) if lines else "（空）"


def _missing_fields(answers: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not answers.get("activity_name"):
        missing.append("活动名称")
    if not answers.get("date"):
        missing.append("活动日期")
    if not (answers.get("start") and answers.get("end")):
        missing.append("活动时间（如 14:00-16:00）")
    if not answers.get("campus"):
        missing.append("校区（鼓楼/浦口/仙林/苏州）")
    if answers.get("people") is None:
        missing.append("预计人数")
    return missing


SESSION_TIMEOUT_SECONDS = 1800  # 30 min


# ---------------------------------------------------------------- 会话


@dataclass
class ApplicationSession:
    user_id: str
    step: str = "collecting"
    answers: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def touch(self) -> None:
        self.updated_at = time.time()

    def is_expired(self) -> bool:
        return (time.time() - self.updated_at) > SESSION_TIMEOUT_SECONDS


# ---------------------------------------------------------------- 管理器


class ConversationManager:

    def __init__(self, *, conversations_dir: Path) -> None:
        self._sessions: dict[str, ApplicationSession] = {}
        self._lock = threading.Lock()
        self._dir = conversations_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._llm_client: llm.LLMClient | None = None

    def _get_llm(self, settings: Settings) -> llm.LLMClient | None:
        if self._llm_client is not None:
            return self._llm_client
        try:
            self._llm_client = llm.LLMClient(
                base_url=getattr(settings, "api_base", "") or "https://api.deepseek.com",
                api_key=settings.api_key,
                model=getattr(settings, "model", "") or "deepseek-chat",
                temperature=0.0,
                max_tokens=1024,
            )
            return self._llm_client
        except Exception:
            return None

    def close(self) -> None:
        if self._llm_client is not None:
            self._llm_client.close()
            self._llm_client = None

    # -- 会话生命周期 ---------------------------------------------------

    def get_or_create(self, user_id: str) -> ApplicationSession:
        with self._lock:
            existing = self._sessions.get(user_id)
            if existing is not None and not existing.is_expired():
                existing.touch()
                return existing
            session = ApplicationSession(user_id=user_id)
            session.created_at = time.time()
            session.touch()
            self._sessions[user_id] = session
            return session

    def get_active(self, user_id: str) -> ApplicationSession | None:
        with self._lock:
            s = self._sessions.get(user_id)
            if s is None:
                return None
            if s.is_expired():
                del self._sessions[user_id]
                return None
            s.touch()
            return s

    def clear(self, user_id: str) -> None:
        with self._lock:
            self._sessions.pop(user_id, None)

    def try_auto_start(
        self, user_id: str, text: str, *, settings: Settings
    ) -> str | None:
        """Try to auto-start a booking session from a natural language message.

        Uses LLM extraction. If any booking fields are found, creates a session
        and returns a reply. Returns None if no booking intent detected.
        """
        client = self._get_llm(settings)
        if client is None:
            return None

        extracted = _extract_with_llm(client, text)
        if not extracted:
            return None

        session = self.get_or_create(user_id)
        self._merge_extracted(session, extracted)
        session.touch()

        # Build response directly (avoid redundant LLM call via _handle_collecting)
        errors = self._collect_errors(session)
        missing = _missing_fields(session.answers)

        if errors:
            return "\n".join(errors) + "\n请重新输入。"

        if missing:
            session.step = "collecting"
            return (
                "已收到。还缺：\n"
                + "\n".join(f"  - {f}" for f in missing)
                + "\n请补充。"
            )

        session.step = "confirm"
        summary = _summary(session.answers)
        return f"信息已收集完毕，请确认：\n\n{summary}\n\n回复「确认」提交，「取消」放弃。"

    # -- 消息处理 -------------------------------------------------------

    def process_message(
        self, user_id: str, text: str, *, settings: Settings
    ) -> tuple[bool, str]:
        """返回 (handled, reply)。"""
        session = self.get_active(user_id)
        if session is None:
            return False, ""

        stripped = text.strip()

        if stripped in ("/cancel", "取消", "放弃"):
            self.clear(user_id)
            return True, "已取消申请。发 /apply 可以重新开始。"

        if not stripped:
            return True, self._prompt_missing(session)

        if session.step == "confirm":
            reply = self._handle_confirm(session, stripped, settings=settings)
        else:
            reply = self._handle_collecting(session, stripped, settings=settings)

        return True, reply

    # -- 智能收集 -------------------------------------------------------

    def _handle_collecting(
        self, session: ApplicationSession, text: str, *, settings: Settings
    ) -> str:
        client = self._get_llm(settings)
        if client is None:
            return "信息提取服务暂时不可用，请稍后再试。"

        extracted = _extract_with_llm(client, text)
        if not extracted:
            missing = _missing_fields(session.answers)
            if not session.answers:
                return "没看懂这条信息，请重新描述你要借教室的需求。"
            return (
                f"没能从这条消息中提取到新信息。还缺：\n"
                + "\n".join(f"  - {f}" for f in missing)
            )

        # 合并到 session
        self._merge_extracted(session, extracted)
        session.touch()

        missing = _missing_fields(session.answers)
        if missing:
            session.step = "collecting"
            return (
                f"已收到。还缺：\n"
                + "\n".join(f"  - {f}" for f in missing)
                + "\n请补充。"
            )

        # 全部收集完毕
        session.step = "confirm"
        summary = _summary(session.answers)
        return f"信息已收集完毕，请确认：\n\n{summary}\n\n回复「确认」提交，「取消」放弃。"

    def _merge_extracted(
        self, session: ApplicationSession, extracted: dict[str, Any]
    ) -> None:
        """把 LLM 提取的字段合并进 session.answers，含校验。"""
        # 活动名
        if extracted.get("activity_name"):
            session.answers["activity_name"] = extracted["activity_name"]

        # 日期
        if extracted.get("date"):
            d, err = _validate_date_str(extracted["date"])
            if err:
                session.answers["_date_error"] = err
            else:
                session.answers["date"] = d.isoformat()
                session.answers.pop("_date_error", None)

        # 时间
        start = extracted.get("start_time", "")
        end = extracted.get("end_time", "")
        if start or end:
            span, err = _validate_time_pair(start, end)
            if err:
                session.answers["_time_error"] = err
            else:
                ksjc, jsjc = span
                session.answers["start"] = start
                session.answers["end"] = end
                session.answers["ksjc"] = ksjc
                session.answers["jsjc"] = jsjc
                session.answers.pop("_time_error", None)

        # 校区
        campus_text = extracted.get("campus", "")
        if campus_text:
            code = school.normalize_campus(campus_text)
            if code:
                session.answers["campus"] = code
                session.answers["campus_name"] = school.CAMPUS_CODES.get(code, campus_text)
            else:
                session.answers["_campus_error"] = (
                    f"认不出校区「{campus_text}」，请从鼓楼/浦口/仙林/苏州中选一个"
                )

        # 人数
        if "people" in extracted:
            session.answers["people"] = extracted["people"]

    def _collect_errors(self, session: ApplicationSession) -> list[str]:
        errors: list[str] = []
        for key in ("_date_error", "_time_error", "_campus_error"):
            err = session.answers.pop(key, None)
            if err:
                errors.append(err)
        return errors

    # -- 确认 -----------------------------------------------------------

    def _handle_confirm(
        self, session: ApplicationSession, text: str, *, settings: Settings
    ) -> str:
        if text in ("确认", "确定", "提交", "是", "ok", "OK", "y", "yes"):
            return self._submit(session, settings=settings)
        if text in ("取消", "放弃", "否", "n", "no"):
            self.clear(session.user_id)
            return "已取消申请。"

        # 用户想修改——重新提取并合并
        client = self._get_llm(settings)
        if client is None:
            return "信息提取服务暂时不可用，请稍后再试。"

        extracted = _extract_with_llm(client, text)
        if extracted:
            self._merge_extracted(session, extracted)
            session.touch()

        errors = self._collect_errors(session)
        missing = _missing_fields(session.answers)
        if errors:
            return "\n".join(errors) + "\n请重新输入。"
        if missing:
            session.step = "collecting"
            return "好，已更新。还缺：\n" + "\n".join(f"  - {f}" for f in missing)
        summary = _summary(session.answers)
        return f"已更新，请重新确认：\n\n{summary}\n\n回复「确认」提交，「取消」放弃。"

    # -- 提交 -----------------------------------------------------------

    def _submit(self, session: ApplicationSession, *, settings: Settings) -> str:
        a = session.answers
        try:
            payload = _build_application_payload(session)
            result = outputs.write_application(settings, payload)
        except outputs.ContractError as exc:
            return f"申请格式校验失败：{exc}\n会话已重置，发 /apply 重新开始。"
        except Exception as exc:
            return f"申请写入失败：{type(exc).__name__}: {exc}\n会话已重置。"

        try:
            _write_accept_notice(
                settings,
                application_id=result["application_id"],
                activity_name=a.get("activity_name", ""),
                activity_date=a.get("date", ""),
                user_id=session.user_id,
            )
        except Exception:
            pass

        self.clear(session.user_id)
        return (
            f"申请已提交！\n"
            f"申请编号：{result['application_id']}\n"
            f"校区：{a.get('campus_name', '')} "
            f"日期：{a.get('date', '')} "
            f"时间：{a.get('start', '')}-{a.get('end', '')}\n"
            f"管理员会收到通知并处理。"
        )

    # -- 提问 -----------------------------------------------------------

    def _prompt_missing(self, session: ApplicationSession) -> str:
        missing = _missing_fields(session.answers)
        if not missing:
            session.step = "confirm"
            summary = _summary(session.answers)
            return f"请确认：\n\n{summary}\n\n回复「确认」提交。"
        return "还需要以下信息：\n" + "\n".join(f"  - {f}" for f in missing)


# ---------------------------------------------------------------- payload


def _build_application_payload(session: ApplicationSession) -> dict[str, Any]:
    a = session.answers
    ksjc = a.get("ksjc", 1)
    jsjc = a.get("jsjc", ksjc)
    people = a.get("people")
    if people is None:
        people = 30

    ts = clock.stamp().replace("-", "").replace(":", "").replace(" ", "_")
    user_hash = hashlib.sha256(session.user_id.encode()).hexdigest()[:8]
    synthetic_doc_id = (
        int(f"{int(date.today().strftime('%Y%m%d'))}{user_hash[:6]}", 16) % (10**9)
    )

    return {
        "source": {
            "repo": "",
            "doc_id": synthetic_doc_id,
            "title": f"QQ申请-{a.get('activity_name', '')}",
            "author": session.user_id[:16],
            "dir": "qq-bot",
            "content_sha256": "",
        },
        "activity": {
            "title": a.get("activity_name", ""),
            "date": a.get("date", ""),
            "period": school.period_label(ksjc, jsjc),
            "people": people,
            "campus": a.get("campus", ""),
            "building": None,
            "room_type": None,
            "preferred_room": None,
        },
        "raw": {
            "activity_name": a.get("activity_name", ""),
            "date": a.get("date", ""),
            "start": a.get("start", ""),
            "end": a.get("end", ""),
            "campus": a.get("campus_name", ""),
            "people": a.get("people", 0),
            "source": "qq_bot",
        },
        "derived": {
            "campus_name": a.get("campus_name", ""),
            "campus_code": a.get("campus", ""),
            "ksjc": ksjc,
            "jsjc": jsjc,
            "people_source": "user_input" if a.get("people") is not None else "default",
        },
        "agent": {
            "run_id": f"qq-{ts}",
            "verdict": "accepted",
            "confidence": "high",
            "notes": ["QQ bot 交互式申请"],
        },
        "normalizations": [],
        "warnings": [],
    }


# ---------------------------------------------------------------- 通知


def _write_accept_notice(
    settings: Settings,
    *,
    application_id: str,
    activity_name: str,
    activity_date: str,
    user_id: str,
) -> dict[str, Any]:
    seq = _next_seq(settings)
    notice_id = _short_hash(f"accepted\x00{application_id}\x00{seq}")
    record = {
        "schema_version": outputs.NOTICE_SCHEMA_VERSION,
        "seq": seq,
        "notice_id": notice_id,
        "created_at": outputs.now_iso(),
        "kind": "accepted",
        "repo": settings.repo,
        "doc": {},
        "member": {"name": ""},
        "summary": f"申请已受理（{application_id}）",
        "message": (
            f"你的教室借用申请已受理。\n"
            f"申请编号：{application_id}\n"
            f"活动：{activity_name}\n"
            f"日期：{activity_date}\n"
            f"管理员会尽快处理，请耐心等待。"
        ),
        "reasons": [],
        "warnings": [],
        "extra": {
            "application_id": application_id,
            "source": "qq_bot",
        },
        "direct_target": {"scope": "c2c", "target_id": user_id},
    }

    path = settings.notify_dir / "pending" / f"{seq:06d}-accepted-{notice_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"seq": seq, "notice_id": notice_id, "path": str(path)}


def _next_seq(settings: Settings) -> int:
    counter = settings.notify_dir / ".seq"
    current = 0
    try:
        current = int(counter.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        current = 0
    for folder in ("pending", "done"):
        d = settings.notify_dir / folder
        if not d.exists():
            continue
        for p in d.glob("*.json"):
            head = p.name.split("-", 1)[0]
            if head.isdigit():
                current = max(current, int(head))
    nxt = current + 1
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(str(nxt), encoding="utf-8")
    return nxt


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


# ---------------------------------------------------------------- 入口


def first_question(session: ApplicationSession) -> str:
    return (
        "开始填写教室借用申请。\n"
        "请告诉我你的需求（有多少信息给多少）：\n"
        "  活动名称、日期、时间段、校区、预计人数\n"
        "例如：「学术讲座 10月5日 14:00-16:00 仙林 50人」\n"
        "也可以分多次告诉我，缺什么我会问你。\n"
        "随时发「取消」退出。"
    )


__all__ = [
    "ApplicationSession",
    "ConversationManager",
    "first_question",
]
