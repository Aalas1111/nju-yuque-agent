# QQBot 接入（`yqa qq …`）

> 一句话：**agent 照旧只做「感知 + 留痕」，QQ 只是它的一个收发口**——
> 把 `outbox/notify/` 里的通知发出去、把别人的命令收进来、
> 以及用手机 QQ 扫一下就把机器人绑好。

这份文档是这一层的维护手册。它讲四件事：

| # | 内容 | 对应代码 |
|---|---|---|
| 1 | 怎么和参考实现对齐 | `src/yuque_agent/qqbot/protocol.py` |
| 2 | 扫码登录（完整流程 + 三种出示方式 + 本地 HTTP 接口） | `login.py` / `login_http.py` / `qr.py` / `credentials.py` |
| 3 | 通知投递（`pending/` → QQ） | `bridge.py` |
| 4 | 入站命令与常驻服务 | `commands.py` / `events.py` / `gateway.py` / `service.py` |

---

## 0. 与参考实现的关系

参考实现：[`qqbot-connector-python`](https://git.nju.edu.cn/qqbot-backend-sdk/qqbot-connector-python)
（PyPI 名 `qqbot-backend-sdk`，`import qqbot_backend_sdk`）。
那个包是 **asyncio** 的，而本项目整体是**同步**的（httpx sync + typer + 常驻轮询）。

**所以这一层是「按同一套协议重写的同步移植」，不是包装。**

| 参考实现 | 本项目 | 说明 |
|---|---|---|
| `auth/qr_session.py` | `qqbot/protocol.py` | `create_bind_task` / `poll_bind_result` / AES-256-GCM 解密 / `build_connect_url` |
| `auth/qr_connect.py` | `qqbot/login.py::QrLoginFlow` | 轮询循环：PENDING 继续等、COMPLETED 解密、EXPIRED 刷新二维码 |
| `auth/qr_login.py` | `qqbot/login.py::QrLoginManager` | 两阶段 `start / wait / cancel`（本层额外提供 HTTP 接口） |
| `auth/token.py` | `qqbot/protocol.py` + `qqbot/client.py` | `bots.qq.com` 取 access_token + 内存缓存 / 提前刷新 |
| `api/client.py` | `qqbot/protocol.py::api_request` | REST 逃生舱（`Authorization: QQBot <token>`） |
| `api/messages.py` | `qqbot/client.py` | 文本 / Markdown 发送、C2C 与群两条路径 |
| `gateway/*` | `qqbot/gateway.py` + `qqbot/events.py` | IDENTIFY / RESUME / 心跳 / 重连 / 事件归一化 |
| —— | `qqbot/bridge.py` | **本项目特有**：`outbox/notify/` 的投递方实现 |
| —— | `qqbot/commands.py` | **本项目特有**：QQ 侧的能力边界（白名单 + 管理员） |
| —— | `qqbot/service.py` | **本项目特有**：agent 线程 + 通知泵 + 网关的编排 |

### 为什么移植而不是 `pip install qqbot-backend-sdk`

1. **同步/异步不混**：本项目从 `httpx.Client` 到 `time.sleep` 全是同步的。
   把网关塞进来已经需要一个事件循环，再让整条链路都 async 化，常驻服务的复杂度会翻倍。
2. **部署不依赖内网 Git**：把它写成 git 依赖，`uv sync` 就必须能访问 NJU GitLab。
   现在只多三个 PyPI 依赖（`cryptography` / `qrcode[pil]` / `websockets`），任何机器都能装。
3. **只实现用得上的部分**：媒体分片上传、流式消息、Webhook 服务、多账户注册表这一层都不要。

### 兼容性（刻意对齐的地方）

* 端点、请求体、`retcode` 语义与参考实现**逐字段一致**；
* 解密算法一致：`key = b64decode(本地 key)`，密文 `IV(12) + data + AuthTag(16)`，AES-256-GCM；
* 状态码一致：`0 NONE / 1 PENDING / 2 COMPLETED / 3 EXPIRED`；
* 凭证文件的键名与参考实现的 `Credentials` 对齐（`appId` / `clientSecret` / `userOpenid`），
  所以 `~/.yuque/qqbot.json` 里的账户可以直接喂给 `QQBotClient.from_credentials`；
* HTTP 接口的返回形状对齐 `start_qr_login` → `{qrDataUrl, message}`、
  `wait_qr_login` → `{connected, message, credentials?}`。

---

## 1. 30 秒上手

**最短路径**——凭证由服务自己扫码拿，第一次启动不用先单独登录：

```bash
uv sync
export YQA_TOKEN=<语雀写权限令牌>
uv run yqa qq config --init                 # 写一份 qqbot.json 模板（可先不填）
uv run yqa qq serve                         # ← 没有凭证时会在终端出示二维码，扫一下继续启动
```

完整路径（想分步来）：

```bash
uv sync                                   # 装依赖（含 cryptography / qrcode / websockets）
uv run yqa qq login                       # ① 手机 QQ 扫码绑定机器人（也可以交给 serve 自动做）
uv run yqa qq config --init               # ② 写一份 qqbot.json 模板
$EDITOR workspace/lqogh0_jsjysq/qqbot.json  # ③ 填成员映射 + 入站白名单
uv run yqa qq doctor                      # ④ 自检
uv run yqa qq notify --dry-run            # ⑤ 看一眼会发什么（不真发）
uv run yqa qq notify                      # ⑥ 真发
uv run yqa qq serve                       # ⑦ 常驻：轮询语雀 + 投递通知 + 接 QQ 命令
```

> **服务启动会自动补登录**（`yqa qq serve` 与 `yqa run --qq` 的默认行为）：
> 找不到 `~/.yuque/qqbot.json` 里的凭证时，先出示二维码（终端里直接画出来）、
> 扫码成功后把 AppSecret 落盘，然后**继续启动服务**；已绑定过则直接用缓存凭证，不再打扰。
> 关掉这个行为用 `--no-login`（`yqa run` 是 `--qq-no-login`）。
> 输出不是终端（systemd / cron / 管道）时**不会**傻等二维码，而是直接给出「怎么先登录」的报错。

只用「发通知」不想收命令？`uv run yqa run --qq` 也能投递（见 §5）。

---

## 2. 扫码登录（完整流程）

### 2.1 流程

```
yqa qq login
   │
   ├─ 1. 本地生成 32 字节随机 key（base64）            ← 只留在内存，绝不外发
   ├─ 2. POST q.qq.com/lite/create_bind_task {key}     → task_id
   ├─ 3. 出示二维码：connect.html?task_id=<task_id>&source=<source>
   │      用户用手机 QQ 扫码 → 手机上点「确认绑定」
   ├─ 4. 每 2 秒 POST q.qq.com/lite/poll_bind_result {task_id}
   │        status=1 PENDING   继续等（不打印噪音日志）
   │        status=2 COMPLETED 用本地 key AES-256-GCM 解密 bot_encrypt_secret → 明文 AppSecret
   │        status=3 EXPIRED   二维码过期 → 回到第 2 步刷新一张新的
   ├─ 5. 凭证写入 ~/.yuque/qqbot.json（权限 600，原子写）
   └─ 6. 可选 --test：立刻取一次 access_token 验证凭证真的可用
```

**为什么 key 要留在本地**：`create_bind_task` 返回的 AppSecret 是用这个 key 加密的，
它只会出现在二维码链接里的 `task_id` 旁边——QQ 侧看不到 key，本地不落 key，
所以即使 `~/.yuque/qqbot.json` 泄露，也只有 AppSecret 泄露，不会波及绑定过程本身。

**失败与重试策略**（都写进了 `login.py`，可用参数覆盖）：

| 情况 | 行为 | 参数 |
|---|---|---|
| 单张二维码没人扫 | 超时后自动刷新一张 | `--timeout 120` |
| 二维码过期 | 立刻刷新 | —— |
| 刷新次数用尽 | 放弃并退出码 1 | `--max-refreshes 6` |
| 接口连续报错 | 退避重试，超过上限才放弃 | 内部 `max_errors=5`、`retry_delay=2s` |
| Ctrl-C | 立刻中止（不留下后台线程） | —— |

### 2.2 三种出示方式

```bash
# ① 终端里直接画（半块字符 + ANSI 黑白配色，手机能扫）
uv run yqa qq login

# ② 存成 PNG，方便贴到聊天窗口 / 服务器上没有终端时用
uv run yqa qq login --png /tmp/qqbot-qr.png

# ③ 起本地 HTTP 接口，让网页/后端去展示（见 2.3）
uv run yqa qq login --http 127.0.0.1:8765

# 老终端画不出颜色 / 不想在终端画
uv run yqa qq login --no-ansi
uv run yqa qq login --no-qr          # 只打印链接，自己复制到手机上打开
```

没装 `qrcode` 也不会失败：退化成「打印链接 + 提示装包」，登录流程照跑。

### 2.3 本地 HTTP 接口（给网页/后端用的「接口」）

```bash
uv run yqa qq login --http 127.0.0.1:8765 [--expose-secret] [--exit-after-login]
```

| 方法 | 路径 | 作用 | 返回 |
|---|---|---|---|
| GET | `/` | 手机上打开就能看到二维码的页面（3 秒自动刷新状态） | HTML |
| GET | `/health` | 存活探测 | `{"ok": true}` |
| POST | `/qr/start` | 开始一次会话 | `{qrDataUrl, qrUrl, state, account, message}` |
| GET | `/qr/status` | 非阻塞查状态 | `{account, state, message, qrUrl, qrDataUrl, connected, appId?}` |
| GET | `/qr/wait?timeout=120` | 阻塞等扫码结果（同一会话只能 wait 一次） | `{connected, message, appId?, credentials?}` |
| POST | `/qr/cancel` | 取消 | `{cancelled, message}` |
| GET | `/qr.png` | 当前二维码原图 | `image/png` |

```bash
curl -s localhost:8765/qr/start | jq -r .qrUrl
curl -s "localhost:8765/qr/wait?timeout=120" | jq
```

**安全默认**（这几条是刻意的，别改）：

* 默认只绑 `127.0.0.1`；绑非回环地址必须同时给 `--allow-remote` 与 `--http-token`，
  否则任何能访问端口的人都能把机器人的 AppSecret 拿走；
* `/qr/wait` **默认不返回 AppSecret**（密钥已经写进本地凭证文件了，没必要过网络）；
  确实需要时加 `--expose-secret`；
* 配了 `--http-token` 之后，每个请求都要带 `X-Auth-Token: <token>`
  （或 `?token=<token>`）。

### 2.4 凭证落在哪

| 项 | 值 |
|---|---|
| 默认路径 | `~/.yuque/qqbot.json`（和语雀 token 的 `~/.yuque/auth.json` 同一目录） |
| 权限 | 文件 `600`、新建的目录 `700`；写是**原子**的（先写 `.tmp` 再 rename） |
| 覆盖路径 | `--credentials <path>` 或环境变量 `YQA_QQ_CREDENTIALS` |
| 多账户 | `--account <name>`；同一个文件里 `accounts.<name>` |

> **`600` 只在 POSIX（Linux / macOS）上真的生效。**
> Windows 的 `chmod` 只能切换只读位、改不动 ACL，而且 `0o600` 不带只读位，
> 所以 `path.chmod(0o600)` 在那上面是**静默成功、什么都没改**，读回来仍是 `666`
> （`scripts/qqbot_sim.py` 打印权限时就会看到这个）。代码本身是对的
> （`credentials.py` → `_chmod_file`），也不是 bug——但请记住两点：
>
> 1. **不要把 Windows 开发机上的权限当安全证据**：正式部署在 Linux 上，那里才是真的 `600`。
> 2. **不会有任何报错提醒你**：这句 `chmod` 在 Windows 上并不抛异常，
>    而 `_chmod_file` 又 `except OSError: pass` 吞掉真失败，
>    所以「没报错」不等于「权限设上了」——上线时请在 Linux 上用 `ls -l` 亲眼确认一次。

```jsonc
{
  "version": 1,
  "accounts": {
    "default": {
      "appId": "102xxxxxx",
      "clientSecret": "……",        // 明文，靠文件权限保护；不进 git、不打日志
      "userOpenid": "……",          // 扫码的那个人
      "boundAt": "2026-09-20T10:18:40+08:00",
      "boundVia": "qr"
    }
  }
}
```

凭证解析优先级：`--app-id/--app-secret` > `YQA_QQ_APPID/YQA_QQ_SECRET` > 凭证文件。
任何地方都不会打印密钥明文——`QQBotAccount.describe()` 只给 `abcd…wxyz（32 位）` 这种掩码。

```bash
uv run yqa qq status        # 看绑定状态（不联网）
uv run yqa qq status --check # 顺便联网验一次 access_token
uv run yqa qq logout --yes   # 删掉本地凭证
```

### 2.5 拿 openid（发消息要用）

`c2c` 与 `group` 的目标都是 openid，不是 QQ 号。三个办法：

1. 跑 `uv run yqa qq serve`，让管理员私聊 bot 发一句 `/status`，
   日志里会打 `[qqbot:in] c2c <openid>: …`；
2. 群里 @ 一下 bot，日志里会打 `[qqbot:in] group <member_openid>: …`，事件里同时带 `group_openid`；
3. 用 REST 逃生舱：`client.api("GET", "/users/@me/guilds")` 之类（要相应权限）。

---

## 3. 通知投递（`outbox/notify/pending/` → QQ）

### 3.1 契约没变，只是有人实现了它

`docs/handoff.md` §3 定义了通知事件的**冻结格式**，并约定投递方：
按 `seq` 升序扫 `pending/` → 发出去 → 把文件**移动**到 `done/`（至少一次语义）。
`bridge.py` 就是那份合同的参考投递方，一个字都没改格式。

### 3.2 目录与状态机

```
outbox/notify/
├── pending/000012-rejected-9f2c1a0b.json   待投递（只读不删）
├── done/   …                               移动成功 = 已投递
├── unrouted/ …                             认不出人（notify.members 里没有）且没有兜底目标
├── failed/   …                             JSON 坏掉 / 没有 message → 挪走，不堵队列
├── .seq                                    程序写的序号计数器（agent 侧）
├── outbox.jsonl                            agent 的审计流水（投递方**不要**读）
└── delivery.jsonl                          投递方的审计流水（每条一行，含状态与目标）
```

| 结果 | 文件去哪 | 下一轮会重试吗 |
|---|---|---|
| 发送成功 | `done/` | 不会（`pending/` 里已经没有它了） |
| 发送失败（网络/限流/权限） | **留在 `pending/`**，本轮**停止**，不越过它去发后面的 | 会 |
| 认不出成员且 `unmapped=skip` | `unrouted/`（等人去 `qqbot.json` 补一条映射） | 不会（补完映射要人工挪回 `pending/`） |
| JSON 坏 / 没有 `message` | `failed/` | 不会 |

> 「发送失败就停下来」是刻意的：合同要求按 `seq` 顺序，越过失败的那条会打乱顺序；
> 而失败本身是暂时的（限流、抖动），下一轮重试就好。

### 3.3 路由规则（身份映射在这一层）

`docs/handoff.md` §3.4 说得很清楚：**agent 给的是语雀侧人名，语雀身份 → QQ 号由 qqbot 负责**。
映射就写在 `qqbot.json` 里：

```
member.name ──精确匹配（忽略大小写）──▶ notify.members[name]
     │ 没匹配上
     ├─ notify.unmapped = default ──▶ notify.default_target（通常是社团群）
     └─ notify.unmapped = skip    ──▶ outbox/notify/unrouted/
```

```bash
uv run yqa qq notify --dry-run     # 只打印「会发给谁」，不发送、不移动
uv run yqa qq notify -n 5          # 本轮最多投 5 条
uv run yqa qq notify --json        # 机器可读的结果
uv run yqa qq notify --file outbox/notify/pending/000012-….json   # 只投一条
```

### 3.4 至少一次与幂等

* 发送成功后**先移文件再记账**，移动是原子的（`Path.replace`，跨设备退回 `shutil.move`）；
* 崩在「发送成功但还没移动」之间 → 重启后会重发一次，这是合同允许的（宁可重复也别漏）；
* 所以下游（社员）可能收到重复消息——真要精确一次，需要业务侧带 `notice_id` 去重，
  这不在本层职责内。

---

## 4. 入站命令（QQ → agent）

### 4.1 能力边界：默认拒绝

和本项目「工具集就是安全闸门」同一立场，QQ 侧的第一道闸门是**配置里的白名单**：

1. `inbound.allow` **为空 = 谁都不能用命令**（不是「默认开放」）；
2. `inbound.admins` 是 `allow` 的子集，只有管理员能触发会花钱/会改知识库的命令；
3. 群聊里还要求**群本身**在 `inbound.groups` 里；
4. 管理员命令有**限流**（默认 30 秒）和**单飞**（agent 正在跑就直接拒绝）；
5. **任何自由文本都不会被送去问 LLM**——回一句「我只认命令」。

### 4.2 命令表

| 命令 | 中文写法 | 谁能用 | 作用 |
|---|---|---|---|
| `/help` | `帮助` | 白名单内所有人 | 列出命令 |
| `/status` | `状态` | 白名单内所有人 | 知识库 / 轮询状态 / 待投递数量 / 最近一轮结论 |
| `/pending` | `待投递` | 白名单内所有人 | 还有几条通知没投出去 |
| `/run` | `跑一轮` | **仅管理员** | 立刻跑一轮轮询（`force=True`，会花 token） |
| `/archive` | `归档` | **仅管理员** | 立刻跑一次归档会话（会改知识库结构） |

斜杠和感叹号前缀都认（`/`、`／`、`!`、`！`），中文别名不打前缀也能用（手机上好按）。

### 4.3 一次 `/run` 的完整链路

```
QQ 群/私聊 ──" /run"──▶ 网关(WS) ──▶ events.parse_event ──▶ CommandRouter
                                                              │ 白名单? 管理员? 限流? 单飞?
                                                              ▼
                                              QQBotService.request_run()  → 入队 + 唤醒
                                                              │
                          （立刻回一句「已排队，跑完发你」＝ 被动回复，必定送达）
                                                              ▼
                       agent 工作线程：runner.poll_once(force=True) → session 留痕
                                                              ▼
                                  结果主动推回发起人（QQ 主动消息有配额，失败只记日志）
```

**为什么用队列而不是直接跑**：整个项目只允许**一个** agent 线程动知识库。
定时轮询、`/run`、`/archive` 全部排进同一个队列串行执行——
两个 LLM run 同时改同一棵目录树，本身就是一种失控。

### 4.4 结果推送的坑

被动回复（带 `msg_id`）只在短时间内有效，而一轮 LLM 可能跑几十秒。
所以策略是：**先被动回执，跑完再主动推**。QQ 对主动消息有较严的配额，
推失败不影响正确性——结论本来就在 `runs/<run_id>/result.json` 里，日志也会记一行。

---

## 5. 常驻服务

```
┌─ 线程：agent 工作循环（唯一跑 LLM 的地方）──────────────────┐
│  队列请求（/run /archive）→ runner.*_once                    │
│  否则 → watcher.tick()（轮询 + 每周六归档）                   │
│  每轮收尾 → bridge.drain()（把通知投出去）                    │
└──────────────────────────┬───────────────────────────────────┘
                           │ outbox/notify/
┌─ 主线程：asyncio ─────────┴───────────────────────────────────┐
│  通知泵：每 notify_interval 秒 drain 一次（默认 5s）           │
│  WS 网关：收 QQ 消息 → CommandRouter → 回一句                  │
└──────────────────────────────────────────────────────────────┘
```

```bash
uv run yqa qq serve                          # 全功能（没凭证会先扫码）
uv run yqa qq serve --no-inbound             # 不收命令（不需要 websockets）
uv run yqa qq serve --no-watch               # 不轮询，只投通知 + 收命令
uv run yqa qq serve --notify-interval 2      # 通知投得更勤
uv run yqa qq serve --dry-run --journal      # 演练/写日志
uv run yqa qq serve --no-login               # 没凭证就直接报错，不要弹二维码（给 systemd/cron）
```

**启动阶段的顺序**（照这个顺序排查最省事）：

1. 检查语雀 token（`YQA_TOKEN`）——缺了直接报错，**不会**让你白扫一次码；
2. 解析 QQBot 凭证：`--app-id/--app-secret` → `YQA_QQ_APPID/YQA_QQ_SECRET` → `~/.yuque/qqbot.json`；
3. 都没有时：stdout 是终端 → 出示二维码 → 扫码 → 落盘 → 继续；不是终端 → 直接报错（`--no-login` 同理）；
4. 起 agent 工作线程 + 通知泵（+ 网关，除非 `--no-inbound`）。

两种常驻姿势的区别：

| | `yqa run --qq` | `yqa qq serve` |
|---|---|---|
| 轮询语雀 | ✅（原来的行为） | ✅（可 `--no-watch` 关掉） |
| 投递通知 | ✅（每轮收尾 drain 一次） | ✅（独立通知泵，默认 5 秒一轮） |
| 收 QQ 命令 | ❌ | ✅（可用 `--no-inbound` 关） |
| 适合 | 只要「社员收到通知」 | 还要「在 QQ 里问状态 / 手动触发」 |

---

## 6. 配置参考（`qqbot.json`）

路径：`<workspace>/<repo_slug>/qqbot.json`，可用 `YQA_QQ_CONFIG` 覆盖。
模板：`examples/qqbot.config.example.json`，或 `yqa qq config --init`。

```jsonc
{
  "version": 1,
  "source": "yuque-agent",          // 会写进二维码链接的 source 参数

  "notify": {
    "unmapped": "default",          // default | skip（见 §3.3）
    "default_target": { "scope": "group", "targetId": "<群 openid>" },
    "members": {
      "张三": { "scope": "c2c",   "targetId": "<user_openid>", "note": "随便写" },
      "李四": { "scope": "group", "targetId": "<group_openid>" }
    }
  },

  "inbound": {
    "enabled": true,                // false = 完全不开入站（回到只投递）
    "allow":  ["<user_openid>"],    // 谁能用命令；空 = 谁都不能
    "admins": ["<user_openid>"],    // 谁能 /run 与 /archive
    "groups": ["<group_openid>"],   // 群聊里要额外把群列进来
    "rate_limit_seconds": 30        // 管理员命令的最小间隔；0 = 不限流
  }
}
```

`yqa qq doctor` 会做**体检**并直接告诉你哪里危险，比如：

* `admins` 非空但 `allow` 为空 → 默认拒绝，没人能用命令；
* 管理员不在 `allow` 里 → 他的命令会被拒；
* `unmapped=default` 但没配 `default_target` → 认不出人的通知会进 `unrouted/`。

## 7. 环境变量

| 变量 | 作用 |
|---|---|
| `YQA_QQ_APPID` / `YQA_QQ_SECRET` | 直接给凭证（CI / 容器里用，优先于凭证文件） |
| `YQA_QQ_ACCOUNT` | 默认账户名（等价 `--account`） |
| `YQA_QQ_CREDENTIALS` | 凭证文件路径（等价 `--credentials`） |
| `YQA_QQ_CONFIG` | `qqbot.json` 路径 |
| `YQA_QQ_INTENTS` | （可选）网关 intents，默认 `1 << 25`（群 @ + C2C 私聊） |

---

## 8. 安全清单

上线前逐条过一遍：

- [ ] `~/.yuque/qqbot.json` 权限是 `600`（在 **Linux 上** `ls -l` 确认——Windows 上查不出来，见 §6 的说明），且**没有**被提交进 git（`.gitignore` 已含 `qqbot.json`，而它本来就在 `~/.yuque/` 下、不在仓库里）；
- [ ] `qqbot.json` 里 `inbound.allow` 只有确实该有权限的人；
- [ ] 会用 `/archive` 的管理员名单最小化（这条命令能删文档、移目录）；
- [ ] 跑 `yqa qq doctor` 没有黄色告警；
- [ ] `--http` 只绑回环；绑外网时同时用了 `--http-token`；
- [ ] 没有随手 `--expose-secret`；
- [ ] `logs` / 终端截图里没有 AppSecret（本层所有输出都过掩码，但仍别手动 print）；
- [ ] `yqa qq serve` 的进程有日志轮转（它对每一条消息都会打一行 `[qqbot:in]`）。

---

## 9. 测试（全部离线）

```bash
uv run pytest tests/test_qq_*.py -v
```

这一层的测试**绝不联网、绝不碰真语雀/真 QQ**：

| 文件 | 覆盖 |
|---|---|
| `test_qq_protocol.py` | 端点/请求体/retcode/状态码映射、AES-GCM 往返解密、REST 鉴权头、HTML 错误页识别 |
| `test_qq_qr.py` | 终端二维码（ANSI 与纯字形）、PNG/data URL、**没装 qrcode 时的降级** |
| `test_qq_login.py` | 扫码成功 / 过期刷新 / 取消 / 超时 / 连续报错 / 两阶段 manager |
| `test_qq_login_http.py` | 真起 socket：`/health` `/qr/start` `/qr/status` `/qr/wait` `/qr/cancel` `/qr.png`、token 鉴权、拒绝非回环绑定 |
| `test_qq_credentials.py` | 落盘与权限、多账户、优先级、掩码与 `repr` 不泄露密钥 |
| `test_qq_client.py` | 目标解析、被动/主动消息体、Markdown、token 缓存与失效 |
| `test_qq_bridge.py` | `done` / `unrouted` / `failed`、顺序、失败留 `pending`、dry-run、审计 |
| `test_qq_events.py` | C2C / 群事件归一化、@ 前缀剥离、无 `message_id` 时不回复 |
| `test_qq_commands.py` | 默认拒绝、白名单、群门禁、管理员、限流、自由文本不进 LLM |
| `test_qq_config.py` | 配置读写、成员解析、兜底、体检 |
| `test_qq_service.py` | 队列与单飞、结果回推、通知泵、入站回复 |
| `test_qq_cli.py` | `config/status/doctor/notify/logout/login` 的 CLI 冒烟（含完整登录落盘） |

假件都在 `tests/qq_fakes.py`（假 transport / 假协议 / 假发送器 / 造通知文件）。

---

## 10. 故障排查

| 现象 | 大概原因 | 怎么办 |
|---|---|---|
| `yqa qq login` 一直「还在等扫码」 | 手机上没确认 / 扫的是旧码 | 看终端里的第几张二维码；过期会自动刷新 |
| 登录成功但 `access_token` 取不到 | AppID/AppSecret 不对，或机器人没在开放平台上线 | `yqa qq status --check`；回开放平台看机器人状态 |
| 通知一直堆在 `pending/` | 没绑定 / 发送失败（限流、权限、主动消息配额） | `yqa qq notify --dry-run` 看目标；看 `delivery.jsonl` 里的 `error` |
| 通知全进了 `unrouted/` | 语雀人名与 `notify.members` 对不上 | `yqa qq notify --dry-run` 会打 `member=…`；补映射后把文件挪回 `pending/` |
| 群里发命令 bot 不理 | 发言人不在 `allow`，或群不在 `groups`；也可能日志里根本没收到事件 | 看 `[qqbot:in]` 日志；`/status` 只在私聊里试一次 |
| 启动服务时提示「没有缓存凭证，而且当前输出不是终端」 | 在 systemd / cron / 重定向里跑，二维码没人看得见 | 先在有终端的地方 `yqa qq login`（凭证可复制过去），或设 `YQA_QQ_APPID/YQA_QQ_SECRET`，或加 `--no-login` 明确不要自动登录 |
| 启动服务时提示「缺少语雀 token」 | 这台机器还没有语雀凭证 | 设 `YQA_TOKEN`；这是**在扫码之前**检查的，避免白扫 |
| 群里任何人都能用 `/run` | `admins` 配太宽 | 收紧 `admins`，并检查 `doctor` 的体检结果 |
| `yqa qq serve` 起来就报 websockets 缺失 | 只装了部分依赖 | `uv sync`；或先 `--no-inbound` 只投通知 |
| 结果消息没推回来 | QQ 主动消息配额/窗口限制 | 属于预期；结论在 `runs/<run_id>/result.json`，或让管理员用 `/status` 查 |
| 终端二维码扫不出来 | 终端字体/缩放导致不成比例 | 用 `--no-ansi`，或 `--png`，或 `--http` 用浏览器扫 |
