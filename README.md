# yuque-agent

> **让 LLM 全权接管一个语雀知识库。** 程序只做「感知 + 留痕」，判断权全部归 LLM。
>
> 这不是一个效率工具，而是一个 **LLM-agent 安全性 / 鲁棒性的实践项目**：
> 研究「怎么做出一个误操作最少、最安全的 agent」。

## ⚠️ 先读这段：为什么又开了一个新项目

上一个项目（`NJU_Yuque` 的 `classroom` 模块）交付了，但结论是**失败的**——
不是功能不行，而是**它把 LLM 去掉了**：时间归一化、节次推算、四档判定、草稿识别
全部硬编码成 Python 规则，LLM 只剩下「调用一个已经写好判断的程序」。

这个项目的立场完全相反：

| | 上一个项目 | 本项目 |
|---|---|---|
| 谁做判断 | Python 规则 | **LLM** |
| 规则怎么表达 | 穷举式代码 | **举例式提示词**（举几个例子，LLM 自己泛化） |
| 程序干什么 | 判断 + 执行 | **只做感知（轮询 diff）与留痕（session / 产出落盘）** |
| 遇到没枚举到的情况 | 崩 / 判错 | **LLM 自己拿主意** |
| 优化目标 | 效率、省 token | **误操作最少、最安全**（token 不是目标） |

> 注：「token 不是目标」≠ 不管 token。语雀手工建一篇文档会分几步产生变更、
> 而程序自己写的《工作日志》又会被当成变更信号——这两个噪声源各自能把一次真实变更
> 放大成十几次 LLM 调用。这类**纯噪声**必须由程序消掉（静默期合并 + 忽略自己写的文档 +
> 占位标题过滤），详见 [`docs/design.md`](docs/design.md) §8。省下来的预算应该花在真正的判断上。

**核心研究问题**：如何做出一个「误操作最少、最安全」的 LLM-agent？

本项目的答案是：

> **安全的第一道闸门是「本次会话注册了哪些工具」（能力边界），而不是提示词里的道德约束。**

日常轮询时，agent 手里**根本没有**删文档 / 移目录的工具（工具集里就没有这几项），
「误删社员的申请」在物理上不可能发生；只有每周六早上的**归档会话**才把这批写工具挂上去。
「能力分级」就是本项目最核心的**可做实验的变量**。

第二个研究问题是**可观测性**：每次 run 的完整 session（喂进去什么 → 它想了什么 → 调了什么工具 →
最后下什么结论）原样留档，并按时间戳写回语雀《工作日志》，供人复查和改进工作流。

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
┌─ 投递层（程序，可选，`yqa qq`）────────────────────────────────┐
│  扫 outbox/notify/pending/ → 按 seq 发到 QQ → 移进 done/        │
│  QQ → agent：白名单命令 /status /pending /run /archive           │
│  扫码登录：`yqa qq login`——手机 QQ 扫一下，AppSecret 自动落盘      │
└────────────────────────────────────────────────────────────────┘
```

详细设计见 [`docs/design.md`](docs/design.md)。

---

## 文档导航

| 文件 | 给谁看 |
|---|---|
| [`docs/deploy.md`](docs/deploy.md) | **给运维**：机器要求、目录布局、systemd 单元、运维注意、上线验收清单 |
| [`docs/handoff.md`](docs/handoff.md) | **给合作方**：两个对外契约（申请 JSON / 通知事件）+ 本 agent 明确的「不做」清单 + 待确认事项 |
| [`docs/design.md`](docs/design.md) | **给维护者**：架构、周期定义、工具分级、提示词写法、踩过的坑 |
| [`docs/qqbot.md`](docs/qqbot.md) | **给 QQBot 接入方**：扫码登录全流程（含本地 HTTP 接口）、通知投递、入站命令的能力边界、故障排查 |
| [`docs/test-report.md`](docs/test-report.md) | **给验收者**：六轮端到端测试（含真服务器部署）的证据、发现的 8 个 bug、复现方式 |
| [`examples/`](examples/) | 申请 / 通知 / `plan.json` / `qqbot.json` 的样例（脱敏），直接看格式最快 |

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
| `YQA_QQ_APPID` / `YQA_QQ_SECRET` | QQ 机器人凭证（跑过 `yqa qq login` 就不用设） | `~/.yuque/qqbot.json` |
| `YQA_QQ_CREDENTIALS` / `YQA_QQ_CONFIG` | QQBot 凭证文件 / `qqbot.json` 的路径 | 工作区默认位置 |

## 用法

```bash
uv run yqa doctor                 # 自检：token / scope / 知识库 / 模型 / 提示词
uv run yqa once                   # 跑一轮轮询（没变化就什么都不做）
uv run yqa once --force           # 无视 diff，强制唤醒一次
uv run yqa once --rescan          # 无视快照，把现有全部文档重新评估一遍（会重发通知）
uv run yqa once --dry-run         # 所有写操作只记录不执行
uv run yqa archive                # 手动跑一次归档会话（有结构写工具）
uv run yqa run --interval 20 --quiet-seconds 45 --journal
                                  # 常驻：轮询 + 静默期合并 + 每周六 00:00 自动归档 + 写工作日志
uv run yqa sessions               # 看本地留了哪些 run
uv run yqa render <run_id>        # 把某次 run 的 session 渲染成人话
uv run yqa journal <run_id>       # 把某次 run 写回语雀《工作日志》
uv run yqa export-plan -o plan.json --defaults '{...}'   # 汇总成下游可直接吃的 plan.json
uv run yqa sync-guide             # 把 kb/guide.md 上传为知识库《指导文档（必读）》
uv run yqa reset-test-data        # 清空测试数据（**默认只预览**，加 --yes 才真删）
                                  #   --scope all 连归档区一起清；--journal 连工作日志也重置
                                  #   《指导文档》《工作日志》永不删；runs/ 默认保留（那是证据）
uv run yqa run --qq               # 常驻轮询，顺带把通知投递到 QQ
```

### QQBot 接入（可选）

```bash
uv run yqa qq login               # 手机 QQ 扫码绑定机器人（也可 --png / --http 127.0.0.1:8765）
uv run yqa qq config --init       # 生成 qqbot.json 模板：成员映射 + 入站白名单
uv run yqa qq doctor              # 自检：凭证 / 配置 / 二维码 / 网关
uv run yqa qq notify --dry-run    # 看会发给谁（不发送、不移动文件）
uv run yqa qq notify              # 把 outbox/notify/pending/ 里的通知投出去
uv run yqa qq serve               # 常驻：轮询语雀 + 投递通知 + 收 QQ 命令
```

> **第一次直接 `yqa qq serve` 就行**：没有 `~/.yuque/qqbot.json` 时它会在终端出示二维码，
> 手机 QQ 扫一下，凭证自动落盘，然后继续启动（`yqa run --qq` 同理）。
> 想关掉这个行为（systemd / cron）加 `--no-login`。

* **通知投递**遵守 [`docs/handoff.md`](docs/handoff.md) §3 的目录协议：
  按 `seq` 升序发；发了就移进 `done/`；认不出人的进 `unrouted/`；坏文件进 `failed/`；
  发送失败留在 `pending/` 下轮重试（至少一次）。
* **运行中的播报**：一次 run 里**只有助手文本会成为消息**（一段一条，不流式），工具调用只用来回答「现在在干什么」；上一条消息之后 `--progress-idle` 秒（默认 60）没动静就发一条「⏳ 正在进行：…」保活。同一条 `msg_id` 用递增 `msg_seq` 连发。`--no-progress` 可关。
* **入站命令**默认拒绝：`qqbot.json` 里的 `inbound.allow` 为空 = 谁都不能用命令；
  `/run` 与 `/archive` 只有 `inbound.admins` 能用，并且限流 + 单飞。
  任何自由文本都不会被送去问 LLM。
* **扫码登录**是完整的官方绑定流程：`create_bind_task` → 出示二维码 → 轮询 →
  AES-256-GCM 解密 AppSecret → 写入 `~/.yuque/qqbot.json`（600）。
  详见 [`docs/qqbot.md`](docs/qqbot.md)。

> **给下游两份契约（申请 JSON / 通知事件）的完整说明见 [`docs/handoff.md`](docs/handoff.md)。**
> 其中 `activity` 对象就是 `crb` 的 `Activity` 原样，`yqa export-plan` 的输出可以直接
> `crb plan --file plan.json --save`。

### 部署（服务器）

```bash
uv sync
uv run yqa doctor                 # 先看自检表，尤其「时区」与「知识库 / 写权限」两行
export YQA_TOKEN=<语雀写权限令牌>      # 或放 ~/.yuque/auth.json
export DEEPSEEK_API_KEY=<key>
uv run yqa run --interval 60 --quiet-seconds 45 --journal   # 要 QQ 就换 yqa qq serve
```

| 项 | 要求 |
|---|---|
| Python | 3.12（用 `uv` 管理） |
| CPU / 内存 | 1 核 1G 够用——这不是算力活，是「等消息」的活 |
| 磁盘 | 5G，但 **`workspace/` 必须持久化**（它存「处理到哪了」，丢了会重复发通知），别放临时盘 |
| **时区** | **不需要配**：程序钉死 `Asia/Shanghai`，服务器是 UTC 也没关系（见 `docs/design.md` §2.2） |
| 出网 | `api.yuque.com`、`api.deepseek.com`（要 QQ 再加 `bots.qq.com`、`api.sgroup.qq.com`） |
| 入网端口 | **不需要开公网端口** |
| 常驻 | 要能长期挂进程（systemd / supervisor / screen），**不能用 serverless** |
| 时钟 | NTP 正常（归档是时钟驱动的） |
| 凭证 | 这台机器上会放**两个**：语雀写权限令牌 + DeepSeek key（都在 `~/.yuque/`） |

> **完整的部署步骤（目录布局 / systemd 单元 / 运维注意 / 验收清单）见
> [`docs/deploy.md`](docs/deploy.md)。**

建议用 systemd 托管并用 `Restart=always`——开发期用 `nohup` 跑时那个进程**自己死过一次**。
QQ 的扫码登录要在**有终端**的地方做一次（`yqa qq login`），凭证可以拷到服务器；
否则用 `--no-login` 明确表示「不要自动登录」，免得在 systemd 里傻等二维码。

---

## 目录

```
src/yuque_agent/
├── config.py     配置与凭证解析 + 工作区路径沙箱（safe_join）
├── yuque.py      语雀 OpenAPI 薄封装（读随便调，写只有 5 个且都过 _write）
├── snapshot.py   快照 + diff → 变更报告（纯函数，离线可测）
├── runner.py     编排：快照 → diff → 唤醒 LLM → 留痕
├── watcher.py    常驻：事件驱动轮询 + 时钟驱动归档（带补跑）
├── agent.py      手搓 agent loop（LLM ↔ tool call）
├── llm.py        OpenAI 兼容客户端 + function calling
├── tools.py      ★ 工具注册表 = 安全闸门（COMMON_TOOLS / ARCHIVE_TOOLS）
├── session.py    session JSONL 留痕（逐行 flush，存原始 reasoning）
├── prompts.py    提示词加载（提示词是 .md 文件，不是字符串常量）
├── prompts/      polling.md / archive.md  ← 项目里最需要反复迭代的东西
├── outputs.py    对外契约：申请 JSON（activity 对齐 crb）/ 通知事件 / export-plan
├── school.py     学校词汇表：校区代码、教学楼代码、节次表、可借日期窗口（查表，不归 LLM）
├── journal.py    session → markdown → 语雀《工作日志》
├── week.py       周目录日期计算
├── kb/           guide.md / template.md（知识库内容源文件）
└── qqbot/        ★ QQBot 接入层（`yqa qq …`）
    ├── protocol.py    QQ 开放平台协议（同步移植参考实现）：扫码绑定 / access_token / REST
    ├── login.py       扫码登录流程 + 两阶段 start/wait/cancel
    ├── login_http.py  本地 HTTP 登录接口（网页/后端可调）
    ├── qr.py          二维码渲染（终端 / PNG / data URL，缺 qrcode 时降级）
    ├── credentials.py 凭证落盘（~/.yuque/qqbot.json，600，多账户）
    ├── client.py      token 缓存 + 发消息（C2C / 群）
    ├── bridge.py      通知投递桥（pending → done/unrouted/failed + 审计）
    ├── config.py      qqbot.json：成员映射 + 入站白名单
    ├── commands.py    入站命令路由（默认拒绝 / 管理员 / 限流）
    ├── events.py      入站事件归一化
    ├── gateway.py     WebSocket 网关（IDENTIFY/RESUME/心跳/重连）
    ├── service.py     常驻编排：agent 线程 + 通知泵 + 网关
    └── cli.py         `yqa qq` 子命令
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
