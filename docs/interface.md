# 接口与边界：核心（yuque-agent）↔ QQ 桥

> **目标态**：两个项目、两个仓库、各自的工作目录与部署路径；**唯一交互面 = 本文档列出的接口**。
> 接口由核心侧（负责人）定义与维护，改动走 PR 到上游 `Aalas1111/nju-yuque-agent`。
>
> **现状（2026-09-25）**：**拆分已完成**。本仓库的 `main` 只保留核心；QQ 桥在它自己的仓库里
> （拆分材料与交接说明在交接分支 `qqbot/handover-20260924` 的 `HANDOVER.md`）。
> §1 的接口**全部实现并在跑**：通知的 `target` 直投、控制请求队列（`once`/`archive`/`apply`）。
> 核心侧不再提供 `yqa qq …` 命令，也不再持有它的 systemd 单元。

## 0. 一句话规则

| # | 规则 | 依据 |
|---|---|---|
| 1 | 核心**不依赖 QQ**：`src/yuque_agent/**` 里没有一行 QQ 代码，也没有它的配置 / 凭证 / 单元（注释与契约文档里提到它是正常的——那是在描述边界） | 边界 |
| 2 | QQ 桥**不 import 核心内部**（`from yuque_agent import …`），只走 §1 的文件接口 | 边界 |
| 3 | **`state.json` 永远只有一个写者**（核心的常驻进程）。桥不许自己起轮询 / 归档，也不许在常驻进程运行时另起 `yqa once` | `AGENTS.md` §2.2、`docs/deploy.md` §11（真事故：两个轮询互相覆盖快照） |
| 4 | 桥的配置、凭证、会话状态放**桥自己的目录**；核心工作区里桥只碰 §1 的接口文件 | 各自工作区 |

## 1. 接口面

### 1.1 通知投递：核心 → 桥（`outbox/notify/pending/`）

* 文件格式：`docs/handoff.md` §3（冻结契约）。
* 桥的动作：扫 `pending/` → 发送 → 成功移 `done/`；认不出目标移 `unrouted/`；
  失败留在原地、下轮重试（顺序按 `seq`，不许跳过）。
* **直投目标**：通知记录里可能带

  ```json
  "target": { "scope": "c2c" | "group", "target_id": "<openid>" }
  ```

  有它就**直接发**，不经过人名映射（用于「在 QQ 里自助申请、回复就该给他本人」这类场景）。
  这是核心正式产出的字段（`outputs.write_notice`）；旧的 `direct_target` 核心不再产出。

### 1.2 控制请求队列：桥 → 核心（`control/requests/`）

* 桥写 `<workspace>/<repo slug>/control/requests/<stamp>-<kind>-<随机>.json`：

  ```json
  { "kind": "once" | "archive" | "apply",
    "requested_by": "<qq 身份>",
    "raw":    { "activity_name": "…", "date": "YYYY-MM-DD", "start": "HH:MM",
                "end": "HH:MM", "campus": "仙林", "people": 25 },
    "target": { "scope": "c2c", "target_id": "<openid>" } }
  ```

  （`raw` 只有 `kind=apply` 才有。）
* 核心常驻进程每轮消费一次（**单写者不变**），回执写回 `control/done/<同名>.json`
  （`{ok, summary, run_id, error}`），请求文件被挪走；`done/` 里超过 7 天的回执自动清理。
* `apply`：校验 / 归一化 / 落盘 / 发通知**全部在核心做**（零 LLM）。桥**不要**自己写
  `outbox/applications/`——绕过核心会漏掉契约校验、索引重建和周期翻转
  （`docs/deploy.md` §9 记过一次同类 bug）。
* 「这一轮跑到哪儿了」：桥只读 tail `runs/<run_id>/session.jsonl`（追加写、逐行 flush）。
  **不要**指望核心给它进程内回调。

### 1.3 只读状态：核心 → 桥

* 允许读：`state.json`、`runs/*/result.json`、`outbox/applications/index.json`、`plan.json`。
* **不要读**：`notes/`（LLM 的跨轮私有记忆）、`plan.defaults.json`（借用人姓名 + 手机号）。
* `yqa status --json` 仍然没有；桥直接读上面那几个文件就够。真要加时以它的输出为准。

## 2. 领地（谁改哪里）

| 路径 | 主人 |
|---|---|
| `src/yuque_agent/**`、`tests/`、`deploy/`、`docs/`（本仓库） | 核心 |
| `outbox/`、`state.json`、`runs/`、`notes/`、workspace 内其它文件 | 核心；桥只读 §1.3，只写 §1.1 的状态转移与 §1.2 的请求文件 |
| 桥的代码 / 单元 / 配置 / 凭证 / 会话状态 | QQ 桥（它自己的仓库与目录） |
| 本文档 | 核心——接口变更以本文档为准 |
