# 接口与边界：核心（yuque-agent）↔ QQ 桥

> **目标态**：两个项目、两个仓库、各自的工作目录与部署路径；
> **唯一交互面 = 本文档列出的接口**。接口由核心侧（负责人）定义与维护，
> 改动走 PR 到上游 `Aalas1111/nju-yuque-agent`。
>
> **现状（2026-09-24）**：§1 里的接口**已全部实现并在跑**（`target` 字段、控制请求
> 队列、apply 入口；QQ 服务不再轮询、不再写 `outbox/` 产物）。物理拆分进行中：
> QQ 桥的代码正迁往它自己的仓库，本仓库的 `main` 将只保留核心。
>
> **过渡期的豁免**（拆完即失效）：桥侧暂时仍 `import` 核心的稳定小模块
> （`clock` / `school` / `llm` / `config.Settings` 的路径属性）——它们是纯工具，
> 物理搬家时替换成自带副本即可。**永不豁免**的是规则 2 里那些造成进程/产物耦合的
> 导入（`runner` / `watcher` / `outputs` / `tools`），它们已经全部切断。

## 0. 一句话规则

| # | 规则 | 依据 |
|---|---|---|
| 1 | 核心**不认识 QQ**：`src/yuque_agent/**`（除 `qqbot/`）不出现任何 QQ 概念 | 边界 |
| 2 | QQ 桥**不 import 核心内部**：不 `from yuque_agent import ...`（runner / watcher / config / clock / outputs / llm / yuque…），只走 §1 的接口 | 边界 |
| 3 | **`state.json` 永远只有一个写者**（核心的常驻进程）。桥不许自己起轮询/归档，也不许在常驻进程运行时另起 `yqa once` | `AGENTS.md` §2.2、`docs/deploy.md` §11（真事故：两个轮询互相覆盖快照） |
| 4 | 桥的配置、凭证、会话状态放**桥自己的目录**（建议 `/var/lib/yuque-notify/`）；核心工作区里桥只碰 §1 的接口文件 | 各自工作区 |

## 1. 接口面

### 1.1 通知投递：核心 → 桥（`outbox/notify/pending/`）【已存在】

* 文件格式：`docs/handoff.md` §3（冻结契约）。
* 桥的动作：扫 `pending/` → 发送 → 成功移 `done/`；认不出目标移 `unrouted/`；
  失败留在原地、下轮重试（顺序按 `seq`，不许跳过）。
* **直投目标字段**（给「不经过语雀人名映射」的接收人，例如 QQ 里自助申请的人本人）：
  通知记录里加

  ```json
  "target": { "scope": "c2c" | "group", "target_id": "<openid>" }
  ```

  这是本文档收编的契约字段；读取时**兼容旧名 `direct_target`**（2026-09-24 由 QQ 桥引入，
  待核心的 `outputs.write_notice` 正式产出该字段后，旧名可择机停用）。
* 桥自己实现搬运（上面就是全部协议），不需要核心的代码。

### 1.2 触发轮询 / 归档：桥 → 核心（控制请求队列）【待核心实现】

* 桥写 `control/requests/<stamp>-<id>.json`（核心工作区下）：

  ```json
  { "kind": "once" | "archive", "requested_by": "<qq 身份>",
    "reply_target": { "scope": "c2c", "target_id": "<openid>" } }
  ```

* 核心常驻进程每轮消费（**单写者不变**），结果落 `runs/<run_id>/`；
  要回执就发一条通知事件到 `pending/`（§1.1）。
* **在它实现之前**：桥不要自己起 `Runner`/`Watcher`，也不要调 `yqa once` 子进程
  （常驻进程在跑时它们会抢 `state.json`）。宁可暂时不提供 `/run`、`/archive`。
* 人肉场景仍用 CLI：`yqa once` / `yqa archive`（**仅当常驻进程没在跑**）。
* 「运行中播报」：桥只读 tail `runs/<run_id>/session.jsonl`（追加写，天然可流），
  不要从核心进程内拿回调。

### 1.3 只读状态：核心 → 桥【部分已存在】

* 允许读：`state.json`、`runs/*/result.json`、`outbox/applications/index.json`、`plan.json`。
* **不要读**：`notes/`（LLM 的跨轮私有记忆）、`plan.defaults.json`（借用人姓名+手机号）。
* 需要的聚合状态由核心补 `yqa status --json`（待加；加了之后桥优先用它）。

### 1.4 提交申请：桥 → 核心【待定，二选一】

交互式申请必须**经核心落盘**（契约校验、`index.json`/`plan.json` 维护、周期翻转都在核心）：

* (a) 核心加 `yqa apply --file <json>`（带契约校验；仅当常驻进程没跑时用）；或
* (b) 桥写 `inbox/apply-requests/<id>.json`，核心常驻消费（**推荐**，与 §1.2 同一套单写者模型）。

在选定并实现之前，桥**不要**直接写 `outbox/applications/`——绕过核心会漏掉
契约校验、索引重建和周期翻转（`docs/deploy.md` §9 记过一次同类 bug）。

## 2. 领地（谁改哪里）

| 路径 | 主人 | 迁移后 |
|---|---|---|
| `src/yuque_agent/**`（除 `qqbot/`） | 核心 | 留在本仓库 |
| `src/yuque_agent/qqbot/**`、`tests/test_qq_*.py`、`tests/qq_fakes.py`、`deploy/yuque-agent-qq.service`、`docs/qqbot.md` | QQ 桥 | **搬到桥自己的仓库**（迁移中） |
| `outbox/`、`state.json`、`runs/`、`notes/`、workspace 内其它文件 | 核心 | 桥只读 §1.3，只写 §1.1 的状态转移与 §1.2/§1.4 的请求文件 |
| 桥的配置 / 凭证 / 会话状态 | QQ 桥 | 放桥自己的目录，不进核心工作区 |
| 本文档 | 核心 | 接口变更以本文档为准 |

## 3. 迁移清单

**QQ 桥侧**：① 整包搬到自己的仓库（独立顶层包名，不再叫 `yuque_agent.qqbot`）；
② 去掉内嵌 agent（`Runner`/`Watcher`/`YuqueClient` 用法），改走 §1.2；
③ 配置/凭证/会话状态搬到自己目录；④ 自己的 `pyproject` + systemd 单元 + 部署脚本，
单元 `ExecStart` 指向自己的代码路径；⑤ 自己补测试（现有 `tests/test_qq_*.py` 一并搬走）。

**核心侧**：① 实现 §1.2 控制队列、§1.4 申请入口、`yqa status --json`、§1.1 的
`target` 字段；② 删 `qqbot/` 及 `cli.py` 的 4 个钩子（`qq_app` 挂载、
`qq_doctor_rows`、`serve --qq` 桥接）；③ 删 `deploy/yuque-agent-qq.service`
与 `tests/test_deploy_doc.py` 的相应断言；④ 更新 `docs/deploy.md` §11、`AGENTS.md` §2.2/§3。

**生产机切换顺序**（不许两台投递者同时跑）：
装好新单元并单独验证 → 停旧的 `yuque-agent-qq.service` → 启用核心的
`yuque-agent.service`（轮询 + 归档回到核心）→ 确认通知在走 → 最后删除旧代码。
**在桥完成迁移之前，谁都不许删本仓库里的 `qqbot/`。**
