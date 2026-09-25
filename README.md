# yuque-agent

> **让 LLM 全权接管一个语雀知识库。** 程序只做「感知 + 留痕」，判断权全部归 LLM。
>
> 这不是一个效率工具，而是一个 **LLM-agent 安全性 / 鲁棒性的实践项目**：
> 研究「怎么做出一个误操作最少、最安全的 agent」。

## 核心研究问题

**如何做出一个「误操作最少、最安全」的 LLM-agent？**

> **安全的第一道闸门是「本次会话注册了哪些工具」（能力边界），而不是提示词里的道德约束。**

日常轮询时，agent 手里**根本没有**删文档 / 移目录的工具，「误删社员的申请」在物理上不可能发生；
只有每周六的**归档会话**才把这批写工具挂上去。「能力分级」就是本项目最核心的**可做实验的变量**。

第二个研究问题是**可观测性**：每次 run 的完整 session（喂进去什么 → 它想了什么 → 调了什么工具 →
最后下什么结论）原样留档，并按时间戳写回语雀《工作日志》，供人复查和改进工作流。

> 背景（上一版项目为什么被判定失败）与设计取舍见 [`docs/design.md`](docs/design.md) §0。

---

## 架构

```
┌─ 感知层（程序，常驻，0 LLM 调用）───────────────────────────────┐
│  轮询：拉 TOC + 文档列表 → 快照 → 与上一轮 diff → 变更报告        │
│        没有变化 → 本轮静默结束                                   │
│  时钟：每周六 00:00（= 周期翻转）→ 无条件唤醒「归档会话」           │
└───────────────────────┬────────────────────────────────────────┘
                        ▼  变更报告 = 纯客观事实，不含任何判定字段
┌─ 判断层（LLM，每次唤醒 = 一个独立 run + 一个独立 session）──────┐
│  prompts/polling.md / prompts/archive.md  ← 举例式，不穷举规则    │
│  工具集按 run 类型分级注册  ← 安全闸门就在这里                     │
│    日常：kb_tree / doc_read / dir_list / ws_* / emit_* / done    │
│    归档：上面 + toc_create / toc_move / toc_remove / doc_delete   │
└───────────────────────┬────────────────────────────────────────┘
                        ▼
┌─ 留痕层（程序）────────────────────────────────────────────────┐
│  runs/<run_id>/session.jsonl  完整留痕（唯一事实来源）            │
│  outbox/applications/*.json   申请（交给负责提交的同学）          │
│  outbox/notify/pending/*.json 通知事件（交给投递层）              │
│  → 渲染成 markdown，按时间戳写回语雀《工作日志》                  │
└───────────────────────────┬────────────────────────────────────┘
                            ▼
┌─ 投递层（**独立项目**：QQ 桥，见 docs/interface.md）──────────────┐
│  扫 outbox/notify/pending/ → 按 seq 发到 QQ → 移进 done/        │
│  QQ 命令：/status /pending /run /apply（后两者写 control/requests/）│
│  扫码登录与凭证在它自己的仓库里管                                  │
└────────────────────────────────────────────────────────────────┘
```

详细设计见 [`docs/design.md`](docs/design.md)。

---

## 文档导航

| 文件 | 给谁看 |
|---|---|
| [`docs/principles.md`](docs/principles.md) | **改代码前必读**：判断归 LLM、程序不越界（禁止在程序层对 LLM 的输出做语义闸门） |
| [`docs/deploy.md`](docs/deploy.md) | **给运维**：机器要求、目录布局、systemd 单元、验收清单、下载口 |
| [`docs/handoff.md`](docs/handoff.md) | **给合作方**：两个对外契约（申请 JSON / 通知事件）+ 本 agent 明确的「不做」清单 + 待确认事项 |
| [`docs/design.md`](docs/design.md) | **给维护者**：架构、周期定义、工具分级、提示词写法、踩过的坑 |
| [`docs/interface.md`](docs/interface.md) | **给 QQ 桥接入方**：接口与边界（两个仓库怎么交互、什么是禁止的） |
| [`docs/test-report.md`](docs/test-report.md) | **给验收者**：端到端测试留下的 bug 台账（现象 → 不变量 → 守卫测试） |
| [`AGENTS.md`](AGENTS.md) | **给在这台机器上干活的代理 / 人**：改代码、部署、排障的规矩（每条都对应一次真实事故） |
| [`examples/`](examples/) | 申请 / 通知 / `plan.json` 的样例（脱敏），直接看格式最快 |

---

## 安装

```bash
uv sync
uv run yqa doctor
```

凭证从环境变量或已有凭证文件读，**永不入库**：

| 变量 | 说明 | 回退 |
|---|---|---|
| `YQA_TOKEN` / `YUQUE_TOKEN` | 语雀 token（归档需要写权限：`repo` + `doc`） | `~/.yuque/auth.json` |
| `YQA_LLM_KEY` / `DEEPSEEK_API_KEY` | LLM API key | `~/.pi/agent/auth.json` |
| `YQA_REPO` | 知识库 namespace，默认 `lqogh0/jsjysq` | |
| `YQA_PLAN_ADMIN` | 「清单已更新」通知发给谁（语雀侧人名，见 `docs/deploy.md` §9） | |

QQ 桥的凭证（`YQA_QQ_*`、`qqbot.json`）归它自己的项目管。

## 用法

```bash
uv run yqa doctor                 # 自检：token / scope / 知识库 / 模型 / 提示词
uv run yqa once                   # 跑一轮轮询（没变化就什么都不做）
uv run yqa once --force           # 无视 diff，强制唤醒一次
uv run yqa once --rescan          # 无视快照，把现有全部文档重新评估一遍（会重发通知）
uv run yqa once --dry-run         # 所有写操作只记录不执行
uv run yqa archive                # 手动跑一次归档会话（有结构写工具）
uv run yqa run --interval 60 --quiet-seconds 45 --journal
                                  # 常驻：轮询 + 静默期合并 + 每周六 00:00 自动归档 + 写工作日志
                                  # ★ 唯一写 state.json 的进程；也消费 control/requests/
uv run yqa sessions               # 看本地留了哪些 run
uv run yqa render <run_id>        # 把某次 run 的 session 渲染成人话
uv run yqa journal <run_id>       # 把某次 run 写回语雀《工作日志》
uv run yqa export-plan -o plan.json --defaults '{...}'   # 汇总成下游可直接吃的 plan.json
uv run yqa serve-plan             # 开申请清单下载口（公开、无密钥，见 docs/deploy.md §10）
uv run yqa sync-guide             # 把 kb/guide.md 上传为知识库《指导文档（必读）》
uv run yqa reset-test-data --workspace <工作区> --yes    # 清空测试数据（默认只预览）
```

> ⚠️ **任何会动工作区的命令都要显式写 `--workspace`**：默认值是相对路径 `workspace`。
> 服务的工作区是 `/var/lib/yuque-agent/workspace`（见 `docs/deploy.md` §6）。
> 工作区落在代码检出里时 `reset-test-data` 会直接拒绝（实测踩到过：语雀里的申请文档照样被删、
> 本地产出却清了个空）。

### QQ 桥（独立项目）

通知投递与 QQ 命令入口是**另一个项目**（2026-09-24 从本仓库拆出）：
它扫 `outbox/notify/pending/` 发 QQ、把 `/status` `/pending` `/run` `/apply` 收进来，
按 [`docs/interface.md`](docs/interface.md) 的接口与核心交互（`/run` 等写 `control/requests/`，
由本仓库的常驻进程执行）。**它不轮询、不写 `outbox/` 产物**——`state.json` 只能有一个写者。
它的代码 / 单元 / 部署 / 文档都在自己的仓库里。

> **给下游两份契约（申请 JSON / 通知事件）的完整说明见 [`docs/handoff.md`](docs/handoff.md)。**
> 其中 `activity` 对象就是 `crb` 的 `Activity` 原样，`yqa export-plan` 的输出可以直接
> `crb plan --file plan.json --save`。

### 部署（服务器）

完整步骤见 [`docs/deploy.md`](docs/deploy.md)。要点：

```bash
uv sync
uv run yqa doctor                 # 先看自检表，尤其「时区」与「知识库 / 写权限」两行
export YQA_TOKEN=<语雀写权限令牌>      # 或放 ~/.yuque/auth.json
export DEEPSEEK_API_KEY=<key>
uv run yqa run --interval 60 --quiet-seconds 45 --journal
```

| 项 | 要求 |
|---|---|
| Python | 3.12（用 `uv` 管理） |
| CPU / 内存 | 1 核 1G 够用——这不是算力活，是「等消息」的活 |
| 磁盘 | 5G，但 **`workspace/` 必须持久化**（它存「处理到哪了」，丢了会重复发通知），别放临时盘 |
| **时区** | **不需要配**：程序钉死 `Asia/Shanghai`，服务器是 UTC 也没关系（见 `docs/design.md` §2.2） |
| 出网 | `api.yuque.com`、`api.deepseek.com`（要 QQ 投递再加 `bots.qq.com`、`api.sgroup.qq.com`） |
| 入网端口 | 只开一个 **8787**（申请清单下载口，公开无鉴权） |
| 常驻 | 要能长期挂进程（systemd / supervisor），**不能用 serverless** |
| 时钟 | NTP 正常（归档是时钟驱动的） |
| 凭证 | 语雀写权限令牌 + DeepSeek key（都在 `~/.yuque/`，600） |

建议用 systemd 托管并用 `Restart=always`——开发期用 `nohup` 跑时那个进程**自己死过一次**。

---

## 目录

```
src/yuque_agent/
├── config.py     配置与凭证解析 + 工作区路径沙箱（safe_join）
├── clock.py      唯一允许碰系统时钟的地方（时区钉死 Asia/Shanghai）
├── yuque.py      语雀 OpenAPI 薄封装（读随便调，写全部过 _write；能力边界一眼可数）
├── snapshot.py   快照 + diff → 变更报告（纯函数，离线可测）
├── runner.py     编排：快照 → diff → 唤醒 LLM → 留痕（`State` 住在这里）
├── watcher.py    常驻：事件驱动轮询 + 时钟驱动归档（带补跑）
├── control.py    控制请求队列：外部（QQ 桥）→ 常驻进程（once / archive / apply）
├── agent.py      手搓 agent loop（LLM ↔ tool call）
├── llm.py        OpenAI 兼容客户端 + function calling
├── tools.py      ★ 工具注册表 = 安全闸门（COMMON_TOOLS / ARCHIVE_TOOLS）
├── session.py    session JSONL 留痕（逐行 flush，存原始 reasoning）
├── prompts.py    提示词加载（提示词是 .md 文件，不是字符串常量）
├── prompts/      polling.md / archive.md  ← 项目里最需要反复迭代的东西
├── outputs.py    对外契约：申请 JSON（activity 对齐 crb）/ 通知事件 / plan.json
├── planserve.py  plan.json 下载口（公开、无密钥；只放行程序算出的那几个文件）
├── school.py     学校词汇表：校区代码、教学楼代码、节次表、可借日期窗口（查表，不归 LLM）
├── journal.py    session → markdown → 语雀《工作日志》
├── week.py       申请周期的日期计算
└── kb/           guide.md / template.md（要投放到知识库的内容源文件）
```

## 开发

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest -v
```

## 免责声明

本项目为社团自用的实践项目，**非南京大学官方项目**。请遵守学校信息系统使用规范与语雀服务协议。

## License

[MIT](LICENSE) © 2026 NOVA Contributors
