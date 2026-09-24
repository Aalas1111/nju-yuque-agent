# 交接说明：把 QQ 桥拆成独立仓库（给接手方 / 给 agent）

> **这份文件是交接入口。** 你是被指来接手的 agent 的话：先读这份，
> 再读 [`docs/interface.md`](docs/interface.md)（接口与边界）和
> [`docs/qqbot.md`](docs/qqbot.md)（QQ 层维护手册），然后照 §3 执行。
>
> 这个分支（`qqbot/handover-20260924`）是**拆分前的最后一个状态**：
> QQ 桥还在本仓库里，但已经**行为解耦**（不轮询、不写核心产物、
> 一切经接口）。你的任务：把它搬进它自己的仓库。

## 0. 这个分支上有什么

| 提交 | 是什么 |
|---|---|
| `f2c069c` | 王恩成 2026-09-24 在生产机上直接改的未提交版本（交互式申请 + RouterAgent）——**原样导出**，没动过一个字 |
| `830ccab` | 解耦改动（维护者侧）：切断内嵌轮询与产物写入，核心补控制队列/apply 入口；修掉验收发现的缺陷（时区、限流、改名同步等） |
| 之后的收尾提交 | 本交接文档 + 文档同步 |

代码状态：`ruff check .` / `ruff format --check .` 干净；`pytest` **526 passed, 3 skipped**。

## 1. 拆分后的目标形状

两个项目、两个仓库、各自部署，**唯一交互面 = `docs/interface.md` 的四个接口**：

```
┌─ 核心仓库（Aalas1111/nju-yuque-agent）───────────────┐
│  轮询 + 归档（唯一写 state.json 的进程）              │
│  产出：outbox/notify/pending/ · outbox/applications/ │
│  消费：control/requests/ → done/（once/archive/apply）│
└──────────────────┬───────────────────────────────────┘
                   │  工作区目录（同机共享）
┌──────────────────┴───────────────────────────────────┐
│  QQ 桥仓库（本分支拆出去的部分）                       │
│  1. 搬到 pending/ 的通知 → 发 QQ → 移 done/           │
│  2. QQ 命令 → /status /pending（本地读）              │
│              /run /archive /apply → 写 control/requests/│
│  不轮询、不写 outbox/ 产物、不 import 核心内部          │
└──────────────────────────────────────────────────────┘
```

## 2. 哪些文件属于 QQ 桥（要搬走的）

```
src/yuque_agent/qqbot/          整包（agent/bridge/client/cli/config/control_client/
                                conversations/credentials/events/gateway/login/
                                login_http/protocol/qr/service）
tests/test_qq_*.py              15 个测试文件 + tests/qq_fakes.py
deploy/yuque-agent-qq.service   QQ 桥的 systemd 单元
docs/qqbot.md                   QQ 层维护手册
scripts/qqbot_sim.py            离线联调脚本
```

**核心侧要留的**：`clock`/`school`/`llm`/`config`（纯工具，桥可以自带副本）、
`outputs` 的**产出格式**（只读文档，不 import）、`control` 的**文件格式**（见下）。

## 3. 执行步骤（agent 照做）

1. **建仓**：从本分支的 `src/yuque_agent/qqbot/` 起一个新仓库，顶层包名改成
   自己的（例如 `nju_qq_bridge`），`pyproject.toml` / `README` / CI 自理。
2. **拆依赖**（规则见 `docs/interface.md` §0）：
   * `from .. import clock` → 自带一份（Asia/Shanghai，见 `tests/test_clock.py` 的约束）；
   * `from .. import school` → 自带（校区代码表 + 节次表；校验以核心为准，桥侧只做交互提示）；
   * `from ..config import Settings` → 换成自带的小设置类（只需要 workspace/repo/paths/api_key）；
   * `from ..llm import LLMClient` → 自带（或换成任意 OpenAI 兼容客户端；`conversations.py` 只用到 `chat()`）；
   * `control_client.py` 里的 `from ..config import Settings` 同上。
3. **入口**：现在靠核心 CLI（`yqa qq serve`）；新仓库要有自己的入口
   （例如 `qq-bridge serve`），参数照现在的 `qq serve`（`--workspace`/`--account`/
   `--credentials`/`--notify-interval`/`--no-inbound`/`--no-login`/`--dry-run`）。
4. **不再做的事**（已经不需要实现）：轮询、归档、分段播报（`progress.py` 已删；
   想要就自己 tail `runs/<run_id>/session.jsonl`）。
5. **配置与凭证**：`qqbot.json`（工作区里）与 `~/.yuque/qqbot.json`（凭证）保持原路径，
   生产机上是这么配的；QQ 侧的会话状态目前是内存态（重启即丢），要持久化就放自己的目录。
6. **单元**：`deploy/yuque-agent-qq.service` 里的 `ExecStart` 改成新入口；
   建议 `SyslogIdentifier=yuque-agent-qq`（保持）；`--no-login` 必须保留。
7. **测试**：`tests/test_qq_*.py` 一并搬走（它们是离线测试，不联网），
   跑通后再补新仓库自己的 CI。
8. **交付前自检**（照核心仓库的 `AGENTS.md` §5 的同等标准）：
   `ruff check . && ruff format --check .` 干净、`pytest` 全绿。

## 4. 接口契约（桥必须按这个来，别再自己发挥）

* **收通知**：`<workspace>/<repo_slug>/outbox/notify/pending/*.json`
  → 发送 → 成功 `done/`；认不出人 `unrouted/`；失败留原地、保 `seq` 顺序。
  记录里可能有 `target: {"scope","target_id"}`（直投，优先于人名映射）。详见 `docs/handoff.md` §3。
* **发请求**：`<workspace>/<repo_slug>/control/requests/<stamp>-<kind>-<随机>.json`
  （`kind` ∈ `once`/`archive`/`apply`）→ 核心消费 → 回执写
  `control/done/<同名>.json`（`{ok,summary,run_id,error}`）。见 `docs/interface.md` §1.2。
* **apply 的字段**（`raw`）：`activity_name` / `date`(YYYY-MM-DD) / `start`(HH:MM) /
  `end`(HH:MM) / `campus`(校区名) / `people`(整数)。校验和落盘在核心，**不要在桥里写
  `outbox/applications/`**。
* **只读允许**：`state.json`、`runs/*/result.json`、`outbox/applications/index.json`、
  `plan.json`。**不要读** `notes/`、`plan.defaults.json`。

## 5. 生产机切换（拆完后，跟维护者一起做）

1. 桥的新仓库部署到 `/opt/qq-bridge`（自己的检出 + venv + 单元）；
2. 停旧的 `yuque-agent-qq.service`（它现在跑的是本仓库的代码）；
3. 确认 `yuque-agent.service`（核心轮询）在跑；
4. 起新单元，验证：`/status` 有回、`/run` 能触发、通知投递正常、`ops.log` 有记录；
5. 两件事**永远不许**同时存在：两个轮询者、两个投递者。
