# 交接文档：给「发起借用」与「通知投递」两位同学

> 本项目（`yuque-agent`）负责 CAC 拆分里的**第一步：确定要素**。
> 它读社员在语雀知识库里写的文档，判定并整理成**结构化事实**，产出两类文件：
> **申请 JSON**（→ 发起借用）与 **通知事件**（→ QQ 投递）。
>
> 本文是这三个模块之间的**冻结合同**。改字段要升 `schema_version` 并通知另外两方。

---

## 0. 一张图

```
社员在语雀写申请文档
        │
        ▼
┌─────────────────────────┐
│  yuque-agent（本项目）    │  只做「自然语言 → 结构化事实」
│  轮询 / 判定 / 归一化     │  不与南大教室系统对接
└───────┬─────────┬───────┘
        │         │
        ▼         ▼
applications/   notify/pending/
   *.json          *.json
        │         │
        ▼         ▼
  「发起借用」     「QQ 投递」
  谷和平           王恩成
  （教室借用插件）  （qqbot）
```

产出目录（默认 `<workspace>/<lqogh0_jsjysq>/outbox/`）：

```
outbox/applications/<application_id>.json    一份受理的申请一个文件
outbox/applications/index.json               索引（扫目录重建）
outbox/notify/pending/<seq>-<kind>-<id>.json 待投递的通知事件
outbox/notify/done/*.json                    投递方挪进来即视为已投递
outbox/notify/outbox.jsonl                   只追加的审计流水（投递方**不要**读它）
```

---

## 1. ⚠️ 本 agent 明确的「不做」清单（请务必读完）

这一节是**能力边界声明**，不是免责声明。下游必须按这个边界设计。

### 1.1 不与学校系统做任何对接

`yuque-agent` **只读语雀**，它**没有**登录南大办事大厅的能力，也**不查**任何教室占用情况。
它不会调用空闲教室接口，也不会提交任何申请。

**因此它无法判断**：

* 社员想要的那间教室在那个时段**是否空闲**；
* 社员想要的那个教学楼在那个时段**有没有任何空教室**；
* 申请提交时教室是否已被别人占掉。

### 1.2 它只做「忠实转发」

一句话：**agent 只负责把社员填的信息收集、整理、结构化，原样交出去。**

具体后果：

| 情况 | agent 的行为 |
|---|---|
| 社员填了教室，但那间教室被占了 | **照样受理并交出**。下游退回「**同一时段、同一教学楼**」的随机空闲教室 |
| 社员填了教学楼，但该教学楼该时段全满 | **照样受理并交出**。下游要么换教学楼、要么按 `no_room` 处理 |
| 社员没填教学楼/教室 | 两个字段留空 = **不限 / 随机**，下游在**同一校区**内按容量挑 |

> **代价要写清楚**：agent 交给下游的申请，**不保证能借到**。
> 「这间教室现在到底空不空」这件事，只有下游（有学校登录态的一方）在**提交那一刻**才能知道。

### 1.3 与旧设计文档的差异（我重新核对《分析1~4》后必须指出的地方）

| 旧文档的说法 | 现状 | 说明 |
|---|---|---|
| 《分析1》建议「把教室借用和 Agent 对接」，理由是 agent 至少要能查空闲教室才能判断该不该退回 | **本版不采用** | CAC 与本轮需求已明确：agent 不做对接。**因此《分析1》里那条「目标教学楼在目标时间段无任何空闲教室 → REJECTED」无法实现**，该判定**下移到下游**（下游查不到教室就按 `no_room` 处理） |
| 《分析1》「不把随机条目的处理放在 agent 阶段」 | **保留** | 原因不变：申请时点与 agent 判定时点脱节，随机结果必须在**提交那一刻**定 |
| 《分析1》「两个活动共用一间教室 → 写在同一篇文档里，处理成一次申请」 | **保留** | 提示词里已写明 |
| 《分析3/4》「agent 全权维护知识库（建库/建文档/移目录/删文档/自愈）」 | **部分采用** | 只有**每周六 00:00 的归档会话**才挂结构写工具；日常轮询**一个语雀写工具都没有**（`tests/test_safety.py` 锁死）。详见 `docs/design.md` §5 |
| 旧版「必须提前 48 小时」 | **修正** | 校方真实规则是**可借日期 = 今天 +2 ~ 今天 +9 天**（来自 谷和平 对借用页面的实测）。`>9 天` 由「受理+提醒」改成了**硬性退回** |
| 旧版「活动日期距今 >14 天 → 受理+提醒」 | **删除** | 被上面的 9 天上限取代 |

---

## 2. 契约 A：申请 JSON（→ 谷和平：发起借用）

### 2.1 核心约定：`activity` 就是 `crb` 的 `Activity`

下游是**纯程序**，它不会替你猜「仙林」是 `"3"`、「仙II区」是 `"11"`。
所以 `activity` 对象的字段名与语义**完全对齐 `NJU_Classroom_Booking`（crb）的 `Activity`**：

| 字段 | 类型 | 说明 | 空值含义 |
|---|---|---|---|
| `title` | str | 活动名称（= 文档标题）。会成为申请的**用途描述** | 必填 |
| `date` | str | 借用日期 `YYYY-MM-DD` | 必填 |
| `period` | str | 节次区间，如 `"7-8"`；单节写 `"7"` | 必填 |
| `people` | int | 人数（社员没填则 30） | 0 = 不筛容量 |
| `campus` | str | **校区代码**：`1` 鼓楼 / `2` 浦口 / `3` 仙林 / `4` 苏州 | 必填 |
| `building` | str \| null | **教学楼代码 `JXLDM`**（如仙林 `"11"`、苏州 `"S06"`） | `null` = **不限/随机** |
| `room_type` | str \| null | 教室类型代码 `JASLXDM` | `null` = 不限 |
| `preferred_room` | str \| null | **意向教室名**，社员原话 | `null` = 随机 |

> **多出来的键会被忽略**（crb 用 pydantic，默认 `extra="ignore"`），所以本文件里的
> `raw` / `derived` / `agent` / `source` 都不会影响消费。

### 2.2 完整示例

```json
{
  "schema_version": "2.0",
  "application_id": "2026-09-23-285808038",
  "created_at": "2026-09-20T10:18:40+08:00",

  "source": {
    "repo": "lqogh0/jsjysq",
    "doc_id": 285808038,
    "title": "新生见面会",
    "author": "张三",
    "dir": "0919-0925",
    "content_sha256": "25dce296…"
  },

  "activity": {
    "title": "新生见面会",
    "date": "2026-09-23",
    "period": "7-8",
    "people": 25,
    "campus": "3",
    "building": "12",
    "room_type": null,
    "preferred_room": "仙I-201"
  },

  "raw": {
    "activity_name": "新生见面会",
    "date": "2026-09-23",
    "start": "16:10", "end": "18:00",
    "campus": "仙林", "building": "仙II区", "room": "仙I-201",
    "people": 25
  },

  "derived": {
    "campus_name": "仙林", "campus_code": "3",
    "ksjc": 7, "jsjc": 8,
    "building_code": "12",
    "building_note": "教学楼「仙II区」→ JXLDM=12",
    "people_source": "document"
  },

  "agent": {
    "run_id": "20260920-101834-polling-7d44",
    "verdict": "accepted",
    "confidence": "high",
    "notes": ["教室未填写，按随机分配处理"]
  },

  "normalizations": ["「下午4点到6点」→ 16:00-18:00"],
  "warnings": []
}
```

### 2.3 怎么用（推荐流程）

#### 取件的是 `outbox/plan.json`（**当前周期**，自动刷新）

**不要自己去拼 `plan.json`，也不要读 `applications/` 目录**：

* `outbox/plan.json` 是**当前申请周期**的交付件；它**每次有申请变动就自动重发**，
  你拿到的总是最新的；
* 周期翻转（周六 00:00）时，程序会把整批产物搬进 `outbox/archive/<周期>/`，
  当前那份 `plan.json` 随之清空。**所以旧申请不会漏进本周的清单**；
* 往期的可以从 `outbox/archive/<周期>/plan.json` 取（那个版本是**冻结**的，
  等于「那周到底交付了什么」的凭证）。

```bash
# 下游先出方案（不写系统）
crb plan --file plan.json

# 确认后落库
crb plan --file plan.json --save
```

> **每次取完请对一眼 `cycle` 字段**（如 `"0919-0925"`），它必须是本周的周期号。
> 这是唯一能一眼看出「我拿到的是不是上周那份」的标记——**服务器不会替你验证**。

#### 两条取件通道

油猴脚本跑在浏览器里，**没法直连服务器**，所以只能人工取：

```bash
# ① scp（零新增攻击面）
scp lihe@<服务器地址>:/var/lib/yuque-agent/workspace/lqogh0_jsjysq/outbox/plan.json .

# ② 网页（输密钥下载）
#    http://<服务器地址>:8787/
#    往期：/archive/0919-0925/plan.json?key=...
```

（服务器地址与密钥由项目负责人单独交接，**不在仓库里**。）

#### 借用人信息（`defaults`）

`JYRXM` / `JYRDH` 这些只需**设一次**：

```bash
uv run yqa export-plan --defaults '{"JYDWDM":"400760","JYRXM":"张三","JYRDH":"13800000000","JSJYLXDM":"02"}'
```

它会落盘到 `outbox/plan.defaults.json`，之后每次自动重发都会带上。
（不落盘不行——自动重发会把当时传的 defaults 丢掉。）

#### 结构

```json
{
  "cycle": "0919-0925",
  "generated_at": "2026-09-20T10:18:40+08:00",
  "defaults": { "JYDWDM": "…", "JYRXM": "…", "JYRDH": "…", "JSJYLXDM": "02" },
  "activities": [
    { "title": "新生见面会", "date": "2026-09-23", "period": "7-8", "people": 25,
      "campus": "3", "building": "12", "room_type": null, "preferred_room": "仙I-201",
      "_application_id": "2026-09-23-285808038", "_doc_id": 285808038 }
  ]
}
```

`activities` 就是 2.1 的 `activity` 原样（加两个 `_` 开头的溯源键）。
`cycle` / `generated_at` 是**新增的元数据**，crb 会忽略（它用 pydantic，
默认 `extra="ignore"`）；留它们是为了让人一眼看出拿到的是哪一周。

#### 想知道当前清单长什么样（在服务器上）

```bash
yqa-as-service export-plan          # 刷新并打印（同时会重写 outbox/plan.json）
```

### 2.4 幂等与去重

* `application_id = <活动日期>-<语雀 doc_id>`：**稳定、可排序、可读**，同一篇文档重复受理只会覆盖同一个文件。
* 一份申请**只写一次**：受理时落盘，之后文档怎么改都不会重写（改了只发 `tampered` 通知）。
  所以下游可以放心「读一个文件 = 处理一次」，但**强烈建议处理完把它挪走**（或记已处理集合）。

---

## 3. 契约 B：通知事件（→ 王恩成：QQ 投递）

### 3.1 目录协议

```
outbox/notify/pending/000012-rejected-9f2c1a0b.json   待投递
outbox/notify/done/000012-rejected-9f2c1a0b.json      投递方挪进来 = 已投递
outbox/notify/unrouted/*.json                         认不出人的（投递方实现，等人补映射）
outbox/notify/failed/*.json                           坏文件（投递方实现，不堵队列）
outbox/notify/outbox.jsonl                            只追加的审计流水
outbox/notify/delivery.jsonl                          投递方的审计流水（投递方实现）
```

**消费约定**：

1. 扫 `pending/`，按文件名前缀 `seq` **从小到大**处理（别解析其余部分）；
2. 一条消息发出去之后，把文件**移动**到 `done/`——移动成功即视为已投递；
3. 崩了重启就重新扫 `pending/`，天然**至少一次**（宁可重复也别漏）；
4. 目录是同机共享的，**qqbot 与 agent 部署在同一台机器上最省事**；
5. `outbox.jsonl` 用来审计，**不要**既读它又挪文件，否则会重复投递。

> 📌 **本仓库现在自带一个投递方实现**（`yqa qq notify` / `yqa qq serve`，
> 代码在 `src/yuque_agent/qqbot/`），上面这套约定一个字都没改。
> 它额外做的三件事：认不出人 → `unrouted/`、坏文件 → `failed/`、发送失败**停下本轮**
> 以保住 `seq` 顺序。身份映射（语雀人名 → QQ openid）放在工作区 `qqbot.json`。
> 详细说明与故障排查见 [`qqbot.md`](qqbot.md)。

### 3.2 字段

```json
{
  "schema_version": "1.0",
  "seq": 12,
  "notice_id": "9f2c1a0b",
  "created_at": "2026-09-20T10:19:24+08:00",
  "kind": "rejected",
  "repo": "lqogh0/jsjysq",
  "doc":    { "doc_id": 285808143, "title": "社团例会", "url": "https://nova.yuque.com/…" },
  "member": { "name": "孙七" },
  "summary": "「社团例会」活动时间起止写反了",
  "message": "「社团例会」这份申请我没法提交：活动时间写的是 17:00-16:00，结束时间比开始时间还早，应该是写反了……",
  "reasons": ["活动时间 17:00-16:00，结束早于开始"],
  "warnings": [],
  "extra": {}
}
```

* **`message` 已经是渲染好的中文正文，QQ 里直接发这一条就行**，不需要下游再拼。
* `summary` 是一句话标题（适合做消息前缀）。
* `reasons` / `warnings` 是结构化版本，想自己排版就用它。

### 3.3 `kind` 取值

| `kind` | 触发 | 建议文案要点 |
|---|---|---|
| `accepted` | 要素齐备、已产出申请 | 附时间/节次/校区；提示「文档已锁定，改也无效」 |
| `rejected` | 判定不通过 | 逐条列原因；强调「改完保存就行，不用做别的动作」 |
| `unrecognized` | 看不出是申请 | 提示按模板新建文档 |
| `tampered` | **已受理**的文档又被改 | 强调「修改无效」；附上原受理信息 |
| `deleted` | **已受理**的文档被删 | 强调「删文档 ≠ 撤回申请」 |
| `info` | 其它需要告知社员的 | —— |
| `plan_updated` | **清单变了**（程序发，见下） | 给 **cac** 的：「请尽快下载并提交」 |

#### `plan_updated`（新增，**只能由程序发**）

每次 `outbox/plan.json` 的内容真变了，程序就发一条这个，提醒 cac 去取件：

```json
{
  "kind": "plan_updated",
  "member": { "name": "<管理员的语雀人名，取自 YQA_PLAN_ADMIN>" },
  "summary": "申请清单已更新（0919-0925）",
  "message": "【申请清单已更新】周期 0919-0925，共 3 条，活动日期 2026-09-23 ~ 2026-09-25。\n请尽快下载 outbox/plan.json 并提交，逾期不补。",
  "extra": { "cycle": "0919-0925", "count": 3, "dates": [...], "plan_fingerprint": "a1b2c3d4" }
}
```

三个刻意的设计：

* **LLM 发不出这一类**。它是程序专属的（`outputs.PROGRAM_NOTICE_KINDS`），
  `emit_notice` 只收 `LLM_NOTICE_KINDS`——不是靠提示词叮嘱。
  因为 LLM 只看得到「这一轮改了哪几篇文档」，看不到清单整体长什么样、有没有过期；
  而「清单变了」是可测的事实，归程序。
* **按内容指纹去重**：一轮里写 3 份申请只发 **1** 条；周期翻转后清单变空**不**发
  （不打扰人）。指纹进了 `state.json`，重启不会重发。
* **文案只说「请下载」，绝不出现「已处理 / 已办结 / 已提交」**。
  我们无从知道 cac 到底下没下、交没交。声称已办结会把「至少一次」
  变成「至多一次」（他一次没下，系统就以为这一批处理过了）。

**投递方需要做的**：在 `notify.members` 里给管理员人名配一个目标，
或者配 `notify.default_target` 兜底。没配也不会丢——会进 `unrouted/` 等人处理。
管理员人名由 `YQA_PLAN_ADMIN` 指定（见 `docs/deploy.md` §10）。

### 3.4 身份映射是投递方的责任

agent 给的是语雀侧身份（`member.name` = 文档里手填的申请人），
**语雀身份 → QQ 号的映射由 qqbot 负责**，agent 不做这个映射。

自带的投递方（`yqa qq notify` / `yqa qq serve`）把这张表放在工作区 `qqbot.json` 的
`notify.members` 里（人名 → `c2c:<user_openid>` / `group:<group_openid>`），
并提供「兜底目标」与「认不出就挪进 `unrouted/` 等人处理」两种策略，
见 [`qqbot.md`](qqbot.md) §3.3。

---

## 4. 需要下游配合 / 待确认的事项

| # | 事项 | 说明 |
|---|---|---|
| H1 | **教学楼名 → `JXLDM` 的完整字典** | 我们只有少数几个（仙林：仙I区=11、仙II区=12、逸夫楼A区=15、逸夫楼B区=16；苏州：南雍楼=S06、公共教学楼=S01），来自上一版对 `jxlcx.do` 的实测。**认不出来时我们留空（= 随机）而不是猜**——猜错的代价比不填大。有学校登录态的一方（插件 / crb）如果愿意，可以按 `raw.building` 自己重新解析。 |
| H2 | **意向教室名要不要模糊匹配** | crb 现在是 `room_name == preferred_room` **精确匹配**。社员写 `仙I-201`、学校库里可能是 `仙Ⅰ-201`（罗马数字），一不匹配就退化成随机。建议下游做一次归一化/模糊匹配。 |
| H3 | **节假日 / 寒暑假 / 校历** | 「今天+2 ~ +9 天」是**自然日**近似。真正的可借窗口受校历与假期影响，agent 不查校历（无学校登录态）。**下游必须在提交前用自己的 `cxxtcs.do` 再校验一次**。 |
| H4 | **一个申请覆盖跨周期日期** | 可借窗口是 9 天，会跨到下一个申请周期（例：9/20 能借到 9/29，而周期目录 `0919-0925` 只到 9/25）。目前**不做特殊处理**——申请按活动日期归档，不按目录归属。 |
| H5 | **提交时机** | 建议下游按**活动日期**而不是「收到申请的时间」排队提交；越接近可借窗口开启越早提交越稳。 |
| H6 | **通知事件的 `kind` 词汇表** | 目前 6 种（见 §3.3）。如果投递方需要新的种类（例如「已通过」「已撤回」），需要在我们的提示词里加例子——`kind` 是 LLM 选的，不是程序枚举的。 |

---

## 5. 快速验证（不依赖真实社员）

```bash
uv sync
export YQA_TOKEN=<语雀写权限令牌>
uv run yqa doctor

# 造一篇测试申请
uv run python scripts/kb_sim.py write --title "新生见面会" \
  --body-file tests/scenarios/t1_standard.md --dir 0919-0925

# 跑一轮，产出 applications/*.json
uv run yqa once

# 汇总成下游格式
uv run yqa export-plan --defaults '{"JSJYLXDM":"02"}'
```

`tests/scenarios/` 下有 8 个场景（标准 / 草稿 / 草稿标记删一半 / 模糊写法 / 时间填反 /
不足 2 天 / 看不出是申请 / 纯自然语言），可以直接拿来跑回归。
