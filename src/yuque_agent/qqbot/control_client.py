"""写控制请求：QQ 桥 → 核心常驻进程（接口见 ``docs/interface.md`` §1.2）。

桥侧只写文件、读回执，**不 import 核心的 runner / watcher / outputs**——
请求格式是双方约定的契约，不是拿对方的私有实现拼出来的。

* 请求：``<workspace>/control/requests/<stamp>-<kind>-<随机>.json``
* 回执：``<workspace>/control/done/<同一个文件名>.json``（由核心写入）
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from .. import clock
from ..config import Settings


def write_control_request(settings: Settings, request: dict[str, Any]) -> str:
    """写一条控制请求，返回文件名（= 回执的文件名）。"""
    directory = settings.control_requests_dir
    directory.mkdir(parents=True, exist_ok=True)
    kind = str(request.get("kind") or "request")
    name = f"{clock.compact_stamp()}-{kind}-{secrets.token_hex(3)}.json"
    path = directory / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return name


def read_control_result(settings: Settings, name: str) -> dict[str, Any] | None:
    """读回执；还没好（或读不出来）返回 ``None``。"""
    path: Path = settings.control_done_dir / name
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def drop_control_result(settings: Settings, name: str) -> None:
    """回执送出去之后删掉（``done/`` 也有 7 天自动清理兜底）。"""
    try:
        (settings.control_done_dir / name).unlink()
    except OSError:  # pragma: no cover
        pass


__all__ = ["drop_control_result", "read_control_result", "write_control_request"]
