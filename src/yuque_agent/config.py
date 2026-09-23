"""配置与凭证解析。

约定：

* 凭证只从**环境变量**或**已有的凭证文件**读；本模块永不写凭证、永不打印凭证；
* 语雀 token 优先级：``YQA_TOKEN`` > ``YUQUE_TOKEN`` > ``~/.yuque/auth.json``；
* LLM key 优先级：``YQA_LLM_KEY`` > ``DEEPSEEK_API_KEY`` > ``~/.pi/agent/auth.json``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_HOST = "https://www.yuque.com"
DEFAULT_REPO = "lqogh0/jsjysq"
DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

PAGE_SIZE = 100  # 语雀文档列表 limit 上限就是 100，超过会 422


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _first(*values: str | None) -> str:
    for value in values:
        if value:
            return value
    return ""


def resolve_yuque_token(explicit: str = "") -> str:
    """语雀 token：显式传入 > 环境变量 > 旧 CLI 登录态文件。"""
    legacy = Path.home() / ".yuque" / "auth.json"
    return _first(
        explicit,
        os.environ.get("YQA_TOKEN"),
        os.environ.get("YUQUE_TOKEN"),
        _read_json(legacy).get("token", ""),
    )


def resolve_llm_key(explicit: str = "") -> str:
    """LLM API key：显式传入 > 环境变量 > pi 的凭证文件。"""
    pi_auth = _read_json(Path.home() / ".pi" / "agent" / "auth.json")
    deepseek = pi_auth.get("deepseek") or {}
    return _first(
        explicit,
        os.environ.get("YQA_LLM_KEY"),
        os.environ.get("DEEPSEEK_API_KEY"),
        deepseek.get("key", "") if isinstance(deepseek, dict) else "",
    )


@dataclass
class Settings:
    """一次进程运行的全部配置。"""

    repo: str = DEFAULT_REPO
    host: str = DEFAULT_HOST
    token: str = ""

    workspace: Path = field(default_factory=lambda: Path("workspace"))
    interval: int = 60
    """轮询间隔（秒）。"""

    quiet_seconds: int = 45
    """**静默期**：发现变化后先不叫醒 LLM，等知识库安静这么久再一次性处理。

    为什么要它：语雀手工建一篇文档会**分几次**产生变更——先是一个无标题空文档，
    接着是改标题，最后才是写正文保存。每一步都是一次 diff，
    不合并的话一篇文档就要唤醒三到五次 LLM（token 翻好几倍）。

    合并之后还有一个额外好处：基线快照在**真正跑完那一轮之前不推进**，
    所以 LLM 看到的是**最终状态**（有标题有正文），而不是中间的半成品；
    而且如果社员建完又立刻删了，静默期结束时 diff 为空，**整轮根本不触发**。

    ``0`` = 关闭合并（发现变化就立刻叫）。
    """

    archive_weekday: int = 5
    """周期起始日（也是归档日）：0=周一 … 5=周六 … 6=周日。"""

    archive_hour: int = 0
    """周期翻转/归档时刻（小时）。周六 00:00 —— 与周期翻转完全重合。"""

    archive_enabled: bool = True

    model: str = DEFAULT_MODEL
    api_base: str = DEFAULT_API_BASE
    api_key: str = ""
    max_steps: int = 24
    """单次 run 里 LLM↔tool 往返步数上限（防死循环）。"""

    max_docs_per_run: int = 50
    """单轮 diff 超过这么多文档就只在报告里列前 N 篇并告警（防恶意批量灌文档）。"""

    max_doc_reads_per_round: int = 200
    """程序每轮最多读多少篇正文（用于算哈希/生成 preview）。"""

    max_tool_calls: int = 60
    """单次 run 的工具调用总次数上限。"""

    dry_run: bool = False
    """True = 所有写操作（语雀 + 工作区）只记录不执行。"""

    journal: bool = False
    """True = 把 session 写回语雀《工作日志》文档。"""

    journal_title: str = "工作日志"

    ignore_doc_titles: tuple[str, ...] = ("工作日志",)
    """**程序自己会写的文档标题。它们的变更永远不算「知识库变了」。**

    否则会出现自激循环：程序写日志 → 日志变了 → 唤醒 LLM → 又写日志 → …
    （实测踩到过：一篇测试文档触发了 13 次 run，其中 8 次就是这个循环，
    每轮白烧 5k token 而且停不下来。）

    注意：只影响**变更检测**，不影响 agent 能不能读到它。
    标题匹配是为了覆盖「首次部署还没记下 doc_id」的情况，
    同时 state 里也会记住 doc_id（防改名）。
    """

    placeholder_titles: tuple[str, ...] = ("无标题", "无标题文档")
    """**语雀自己生成的占位标题。标题是它、且正文为空 → 根本不算变更。**

    「无标题」是官方 API 在 title 为空时自动填的（实测），
    「无标题文档」是网页端新建文档时的默认标题——两个都收。

    这是比草稿标记**更早一级的筛子**：草稿标记至少是人主动删的，
    占位标题连人都没碰过，不可能携带借用意图。
    一旦标题被改成任何其他写法（哪怕是错字「无标题文」），筛选立刻失效 → 照常交给 LLM，
    而且仍然走静默期合并。

    正文一有内容也不算中间态（防止「没改标题直接粘正文」的真实申请被静默丢掉）。
    """

    verbose: bool = False

    plan_admin: str = ""
    """「申请清单已更新」这条通知发给谁（**语雀侧人名**）。

    它必须是人名而不是 QQ 号：本层不认识 QQ 身份，映射在 ``qqbot.json`` 的
    ``notify.members`` 里（这是当初分层时定的——agent 只知道语雀侧的人）。

    留空 = 走 ``notify.default_target`` 兜底；连兜底也没配就进 ``unrouted/``
    等人处理（**不会默默丢掉**）。
    """

    @classmethod
    def from_env(cls, **overrides: Any) -> Settings:
        settings = cls(
            repo=os.environ.get("YQA_REPO", DEFAULT_REPO),
            host=os.environ.get("YQA_HOST", DEFAULT_HOST),
            workspace=Path(os.environ.get("YQA_WORKSPACE", "workspace")),
            interval=int(os.environ.get("YQA_INTERVAL", "60")),
            model=os.environ.get("YQA_MODEL", DEFAULT_MODEL),
            api_base=os.environ.get("YQA_API_BASE", DEFAULT_API_BASE),
            plan_admin=os.environ.get("YQA_PLAN_ADMIN", ""),
        )
        for key, value in overrides.items():
            if value is not None:
                setattr(settings, key, value)
        settings.token = resolve_yuque_token(settings.token)
        settings.api_key = resolve_llm_key(settings.api_key)
        return settings

    # -- 派生路径 ---------------------------------------------------------
    @property
    def slug(self) -> str:
        """``lqogh0/jsjysq`` → ``lqogh0_jsjysq``（用作目录名）。"""
        return self.repo.replace("/", "_")

    @property
    def root(self) -> Path:
        return self.workspace / self.slug

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def applications_dir(self) -> Path:
        return self.root / "outbox" / "applications"

    @property
    def outbox_dir(self) -> Path:
        return self.root / "outbox"

    @property
    def plan_file(self) -> Path:
        """**当前周期**的交付件。每次申请变动就重写它（下游 cac 就取这个文件）。"""
        return self.outbox_dir / "plan.json"

    @property
    def plan_defaults_file(self) -> Path:
        """借用人信息（``JYRXM`` / ``JYRDH`` …）。**不随周期归档**——它不是周期性的。"""
        return self.outbox_dir / "plan.defaults.json"

    @property
    def archive_dir(self) -> Path:
        """往期产物，按周期分目录（``archive/0919-0925/``）。"""
        return self.outbox_dir / "archive"

    def cycle_archive_dir(self, cycle: str) -> Path:
        """某个周期的归档目录。**和活跃期的结构完全同形**（plan.json + applications/），
        所以「这周交付了什么」以后能原样翻出来。"""
        return self.archive_dir / cycle

    @property
    def notify_dir(self) -> Path:
        return self.root / "outbox" / "notify"

    @property
    def notes_dir(self) -> Path:
        """LLM 唯一允许自己写的目录（跨轮记忆）。"""
        return self.root / "notes"

    def ensure_dirs(self) -> None:
        for path in (
            self.root,
            self.runs_dir,
            self.applications_dir,
            self.notify_dir / "pending",
            self.notify_dir / "done",
            self.notes_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def safe_join(root: Path, relative: str) -> Path:
    """把 ``relative`` 拼到 ``root`` 下，并确保结果没有越界。

    越界（``../``、绝对路径、盘符、符号链接逃逸）一律抛 :class:`ValueError`，
    由调用方把错误原样返回给 LLM，让它自己纠正。
    """
    if not relative or relative.strip() == "":
        raise ValueError("路径不能为空")
    candidate = (root / relative).resolve()
    base = root.resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError(f"路径越界：{relative!r} 不在工作区允许范围内")
    return candidate
