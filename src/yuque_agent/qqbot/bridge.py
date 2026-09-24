"""通知投递桥：``outbox/notify/pending/*.json`` → QQ。

``docs/handoff.md`` §3 把「通知事件」定义成一份**冻结合同**，并约定：

1. 扫 ``pending/``，按文件名前缀 ``seq`` **从小到大**处理；
2. 一条发出去之后把文件**移动**到 ``done/``——移动成功即视为已投递；
3. 崩了重启就重新扫 ``pending/``，天然**至少一次**（宁可重复也别漏）。

这个模块就是那份合同的**参考投递方实现**（原来计划交给「QQ 投递的同学」）。
本层只加了三件合同里没写但必须有的东西：

* ``unrouted/``：认不出人（``notify.members`` 里没有）又没兜底目标的，**挪过去等人处理**，
  绝不静默丢弃；
* ``failed/``：JSON 都读不出来的坏文件挪过去，避免它永远堵在队首；
* ``delivery.jsonl``：投递侧的审计流水（agent 的 ``outbox.jsonl`` 是 agent 自己的，
  两边各记各的，互相当成「真相」会重复投递）。

发送失败时**保持文件在 ``pending/``**，并且**停下本轮**（不越过它去发后面的），
这样 seq 顺序不会被破坏，下一轮自然重试。
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import clock
from .client import MessageSender
from .config import QQBotConfig

AUDIT_NAME = "delivery.jsonl"

STATUS_DELIVERED = "delivered"
STATUS_DRY_RUN = "dry_run"
STATUS_UNROUTED = "unrouted"
STATUS_FAILED = "failed"


@dataclass
class DeliveryResult:
    """一条通知的投递结果。"""

    path: Path
    seq: int
    notice_id: str = ""
    kind: str = ""
    status: str = STATUS_DELIVERED
    target: str = ""
    member: str = ""
    reason: str = ""
    error: str = ""
    moved_to: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_DELIVERED, STATUS_DRY_RUN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "seq": self.seq,
            "notice_id": self.notice_id,
            "kind": self.kind,
            "status": self.status,
            "target": self.target,
            "member": self.member,
            "reason": self.reason,
            "error": self.error,
            "moved_to": str(self.moved_to) if self.moved_to else "",
        }

    def describe(self) -> str:
        head = f"#{self.seq:06d} {self.kind or '?'} {self.notice_id or ''}".strip()
        if self.status == STATUS_DELIVERED:
            return f"{head} → {self.target}（已投递）"
        if self.status == STATUS_DRY_RUN:
            return f"{head} → {self.target}（dry-run，未发送）"
        if self.status == STATUS_UNROUTED:
            return f"{head} → 没人可发（{self.reason}），已挪到 unrouted/"
        return f"{head} 投递失败：{self.error or self.reason}"


class NotifyBridge:
    """待投递通知 → QQ 的搬运工。线程安全（常驻服务里两个线程都会调 :meth:`drain`）。"""

    def __init__(
        self,
        *,
        notify_dir: str | Path,
        sender: MessageSender,
        config: QQBotConfig,
        dry_run: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.notify_dir = Path(notify_dir)
        self.sender = sender
        self.config = config
        self.dry_run = dry_run
        self.log = log
        self._lock = threading.Lock()

    # -- 目录 -------------------------------------------------------------
    @property
    def pending_dir(self) -> Path:
        return self.notify_dir / "pending"

    @property
    def done_dir(self) -> Path:
        return self.notify_dir / "done"

    @property
    def unrouted_dir(self) -> Path:
        return self.notify_dir / "unrouted"

    @property
    def failed_dir(self) -> Path:
        return self.notify_dir / "failed"

    @property
    def audit_path(self) -> Path:
        return self.notify_dir / AUDIT_NAME

    def ensure_dirs(self) -> None:
        for path in (self.pending_dir, self.done_dir, self.unrouted_dir, self.failed_dir):
            path.mkdir(parents=True, exist_ok=True)

    # -- 查询 -------------------------------------------------------------
    def pending(self) -> list[Path]:
        """``pending/`` 里的文件，按 ``seq`` 升序（解析不出 seq 的排最后）。"""
        if not self.pending_dir.exists():
            return []
        return sorted(self.pending_dir.glob("*.json"), key=_sort_key)

    def pending_count(self) -> int:
        return len(self.pending())

    def stats(self) -> dict[str, int]:
        def count(path: Path) -> int:
            return len(list(path.glob("*.json"))) if path.exists() else 0

        return {
            "pending": count(self.pending_dir),
            "done": count(self.done_dir),
            "unrouted": count(self.unrouted_dir),
            "failed": count(self.failed_dir),
        }

    # -- 投递 -------------------------------------------------------------
    def drain(self, *, limit: int = 20) -> list[DeliveryResult]:
        """把 ``pending/`` 里能发的发掉。返回本轮每条的处理结果。

        遇到**发送失败**就停：保住 seq 顺序，剩下的下一轮再来（至少一次）。
        """
        with self._lock:
            self.ensure_dirs()
            results: list[DeliveryResult] = []
            for path in self.pending()[: max(0, limit)]:
                result = self._deliver_locked(path)
                results.append(result)
                if result.status == STATUS_FAILED and not result.moved_to:
                    # 发送失败：停在原地，不越过它
                    break
            return results

    def deliver(self, path: str | Path) -> DeliveryResult:
        """投递指定的一个文件（``yqa qq notify --file`` 用）。"""
        with self._lock:
            self.ensure_dirs()
            return self._deliver_locked(Path(path))

    # -- 内部 -------------------------------------------------------------
    def _deliver_locked(self, path: Path) -> DeliveryResult:
        result = DeliveryResult(path=path, seq=_seq_of(path), status=STATUS_FAILED)

        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result.error = f"读不出通知 JSON：{exc}"
            result.reason = "malformed"
            self._move(path, self.failed_dir, result)
            self._audit(result)
            self._log(f"[qqbot:notify] {path.name} 读不出来，挪到 failed/：{exc}")
            return result
        if not isinstance(record, dict):
            result.error = "通知不是 JSON 对象"
            result.reason = "malformed"
            self._move(path, self.failed_dir, result)
            self._audit(result)
            return result

        result.seq = int(record.get("seq") or result.seq)
        result.notice_id = str(record.get("notice_id") or "")
        result.kind = str(record.get("kind") or "")
        result.member = str((record.get("member") or {}).get("name") or "")

        message = str(record.get("message") or "").strip()
        if not message:
            result.error = "通知里没有 message（合同要求 message 可以直接发出去）"
            result.reason = "empty_message"
            self._move(path, self.failed_dir, result)
            self._audit(result)
            return result

        # 直投目标：不经过人名映射，直接发给通知里指定的 QQ（契约字段 ``target``，
        # 见 docs/handoff.md §3、docs/interface.md §1.1；兼容旧名 ``direct_target``）。
        direct = record.get("target")
        if not (isinstance(direct, dict) and direct.get("scope") and direct.get("target_id")):
            direct = record.get("direct_target")
        if isinstance(direct, dict) and direct.get("scope") and direct.get("target_id"):
            from .client import Target as _Target

            _t = _Target(str(direct["scope"]), str(direct["target_id"]))
            target = _t  # Target object directly
            basis = "direct_target"
            send_target = _t
        else:
            nt, basis = self.config.resolve_member(result.member)
            if nt is None:
                result.status = STATUS_UNROUTED
                result.reason = basis
                self._move(path, self.unrouted_dir, result)
                self._audit(result)
                self._log(
                    f"[qqbot:notify] {path.name}：语雀成员 {result.member or '(空)'} 认不出，"
                    f"挪到 unrouted/（在 qqbot.json 的 notify.members 里加一条即可）"
                )
                return result
            target = nt  # NotifyTarget
            send_target = nt.target  # Convert to Target for send_text

        result.target = target.to_str() if hasattr(target, "to_str") else str(target)
        if self.dry_run:
            result.status = STATUS_DRY_RUN
            self._audit(result)
            self._log(f"[qqbot:notify][dry-run] {path.name} → {result.target}")
            return result

        try:
            self.sender.send_text(send_target, message)
        except Exception as exc:  # noqa: BLE001 - 任何发送失败都留在 pending 重试
            result.status = STATUS_FAILED
            result.error = f"{type(exc).__name__}: {exc}"
            self._audit(result)
            self._log(
                f"[qqbot:notify] {path.name} 发送失败（留在 pending 下轮重试）：{result.error}"
            )
            return result

        result.status = STATUS_DELIVERED
        self._move(path, self.done_dir, result)
        self._audit(result)
        self._log(f"[qqbot:notify] {path.name} → {result.target} 已投递（{basis}）")
        return result

    def _move(self, path: Path, folder: Path, result: DeliveryResult) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / path.name
        try:
            path.replace(destination)
        except OSError:
            # 跨设备（比如 outbox 挂在另一个文件系统）时 replace 会失败，退回 shutil.move
            shutil.move(str(path), str(destination))
        result.moved_to = destination

    def _audit(self, result: DeliveryResult) -> None:
        try:
            self.notify_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "at": clock.stamp(),
                **result.to_dict(),
            }
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:  # pragma: no cover - 审计写不了不该影响投递
            pass

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log(text)


def _seq_of(path: Path) -> int:
    head = path.name.split("-", 1)[0]
    return int(head) if head.isdigit() else 0


def _sort_key(path: Path) -> tuple[int, str]:
    head = path.name.split("-", 1)[0]
    if head.isdigit():
        return (int(head), path.name)
    return (1 << 30, path.name)  # 解析不出 seq 的放最后


def delivery_target_preview(record: dict[str, Any]) -> str:
    """调试用：只看不能发时缺什么。"""
    member = str((record.get("member") or {}).get("name") or "")
    return f"member={member or '(空)'}"


__all__ = [
    "AUDIT_NAME",
    "STATUS_DELIVERED",
    "STATUS_DRY_RUN",
    "STATUS_FAILED",
    "STATUS_UNROUTED",
    "DeliveryResult",
    "NotifyBridge",
    "delivery_target_preview",
]
