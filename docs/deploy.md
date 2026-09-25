# 部署（服务器）

> 本文件只讲**怎么在一台新机器上把它跑起来**，不含任何服务器地址与凭证。
> 具体那台机器的信息（IP / 账号 / 凭证位置）由项目负责人单独交接，**不要写进公开仓库**。

这套东西的负载极轻：**一个常驻 Python 进程，每 60 秒拉一次语雀、偶尔调一次 LLM**。
实测内存占用约 **32 MB**，CPU 基本空转。所以机器不需要多好，但要满足下面几条。

## 1. 机器要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Linux（Ubuntu 22.04 / 24.04 实测可用） | 需要 `systemd` |
| Python | **3.12**（用 `uv` 管，不依赖系统版本） | Ubuntu 24.04 自带 3.12；22.04 是 3.10 也能跑，`uv` 会自己下 3.12 |
| 内存 / CPU | 1 核 1G 起，2 核 2G 舒服 | 这不是算力活，是「等消息」的活 |
| 磁盘 | 10G 起，`workspace/` **必须持久化** | 它存「处理到哪了」，丢了会重复发通知。别放 tmpfs |
| **时区** | **不需要配** | 程序把时区**钉死**成 `Asia/Shanghai`（见 `docs/design.md` §2.2），服务器是 UTC 也没关系 |
| 出网 | `api.yuque.com`、`api.deepseek.com`（QQ 桥还要 `bots.qq.com`、`api.sgroup.qq.com`，那是它自己的事） | 还要能拉代码（见 §5 的注意） |
| 入网端口 | **只开一个：8787**（申请清单下载口，**公开、无鉴权、打开即下载**，见 §10） | 别绑域名（见下面那段备案的话） |
| 常驻能力 | 要能**长期挂进程**（systemd / supervisor）。**不能用 serverless / 函数计算** | 归档是时钟驱动的，进程不能被回收 |
| 时钟 | NTP 正常 | 「每周六 00:00 归档」靠它 |
| GPU | 不需要 | LLM 走 DeepSeek 的 API |

> **不需要 ICP 备案**——前提是**别绑域名**。用 `公网IP:端口` 访问即可。
> 一旦用域名对外提供 web 服务，境内服务器就要备案（几周）。

## 2. 目录布局

```
/opt/yuque-agent/                    代码：git clone（root 所有，服务只读）
├── src/yuque_agent/                 核心模块（QQ 桥已拆成独立项目，见 docs/interface.md）
│   ├── prompts/                     提示词：polling.md · archive.md
│   └── kb/                          投放到语雀的：guide.md · template.md
├── tests/  docs/  scripts/  examples/
└── .venv/                           依赖（预建；服务用 --no-sync，不写它）

/var/lib/yuque-agent/                运行状态（yuque 用户所有）
├── workspace/<group>_<repo>/        每个知识库一个工作区
│   ├── state.json                   快照 + 轮询状态
│   ├── runs/<stamp>-<kind>-<id>/    每次会话一个目录
│   │   ├── payload.json             喂给 LLM 的输入（含当时的提示词，留底）
│   │   ├── session.jsonl            逐条留痕：工具调用 / 返回 / 思考
│   │   ├── result.json              这轮的结构化结果
│   │   └── journal.json             要写回《工作日志》的内容
│   ├── control/                     控制请求队列：外部（QQ 桥）→ 核心常驻进程
│   │   ├── requests/                请求（once / archive / apply），核心消费
│   │   └── done/                    回执（核心写，请求方读），7 天自动清理
│   ├── outbox/
│   │   ├── applications/index.json  给下游（crb）的申请索引
│   │   └── notify/                  QQ 通知桥
│   │       ├── pending/  done/      状态机（unrouted/ failed/ 按需建）
│   │       ├── outbox.jsonl         追加日志
│   │       └── .seq                 全局序号
│   └── notes/                       LLM 的跨轮记忆（格式它自己定，不解析）
└── .uv-cache/                       uv 缓存（服务与 yqa-as-service 共用这份）

/home/yuque/.yuque/                  凭证（目录 700，文件 600，yuque 所有）
├── auth.json                        {"token": "<语雀写权限令牌>"}
└── agent.env                        DEEPSEEK_API_KEY=<key>

/etc/systemd/system/yuque-agent.service
/etc/sudoers.d/<协作账号>             按需（给协作方开账号时才建）
/usr/local/bin/yqa-as-service        手动调试用的包装脚本（见 §6）
/var/log/journal/                    服务日志（journald，已设持久化）
```

**为什么分这么细**：代码只读、状态可写、凭证最紧——服务不需要写自己的代码，
也不需要碰别人的东西。即使这个进程被攻破，能改的也只限于它自己的状态目录。

**工作区目录名 = `<group>_<repo>`**（`config.Settings.slug`，把 `/` 换成 `_`）。
不是硬编码知识库名——同一台机器上跑第二个知识库时状态不会互相覆盖。

**`runs/` 是唯一的事实来源**：那四个文件合起来能还原「那一轮到底发生了什么」。
`state.json` 和 `outbox/` 都是从它派生的，所以清理测试数据时**默认不动 `runs/`**
（见 §7 的 `reset-test-data`）。

## 3. 安装

```bash
# ① 系统级装 uv（供所有用户用；装到 ~/.local/bin 的话别的用户读不到）
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# ② 建专用用户（不给 shell —— 它只需要跑服务，不需要登录）
useradd -r -m -d /home/yuque -s /usr/sbin/nologin yuque

# ③ 拉代码
git clone https://github.com/Aalas1111/nju-yuque-agent /opt/yuque-agent
cd /opt/yuque-agent && uv sync

# ④ 放凭证（路径见 §2；权限 600，属主 yuque）
install -d -m 700 -o yuque -g yuque /home/yuque/.yuque
printf '{"token": "<语雀写权限令牌>"}\n' > /home/yuque/.yuque/auth.json
printf 'DEEPSEEK_API_KEY=<key>\n'        > /home/yuque/.yuque/agent.env
chown yuque:yuque /home/yuque/.yuque/* && chmod 600 /home/yuque/.yuque/*

# ⑤ 状态目录
install -d -m 750 -o yuque -g yuque /var/lib/yuque-agent

# ⑥ 先验一遍（离线，不碰真语雀）
cd /opt/yuque-agent && sudo -u yuque env HOME=/home/yuque uv run --no-sync pytest -q
```

## 4. systemd 单元（轮询 + 归档：唯一写 state.json 的那个）

> **轮询只有一个写者**：`state.json` 由本单元独占写。QQ 桥（独立项目，见 §11）
> 只投递通知 + 处理命令、不轮询；任何外部进程都不许自己起轮询。

**权威副本在仓库里：`deploy/yuque-agent.service`。** 直接装它，别手工粘贴——
当初这份文档就是手工维护的，结果和真机漂了（少了 `Documentation=` 和
`SyslogIdentifier=`，`ExecStart` 也不一样），照着敲出来的单元
**和正在跑的不是同一个文件**。

```bash
# 仓库就在机器上，直接 install（这是推荐做法）
install -m 644 /opt/yuque-agent/deploy/yuque-agent.service \
    /etc/systemd/system/yuque-agent.service
systemctl daemon-reload && systemctl enable --now yuque-agent
```

单元内容以仓库里那份为**唯一权威**（`deploy/yuque-agent.service`）。
`tests/test_deploy_doc.py` 不再把内容抄进本文档，只钉住几条真出过事的关键指令
（`SuccessExitStatus=143`、`EnvironmentFile=`、`User=yuque`、加固开关…）——
文档抄一份的下场是**它自己先漂**（实测过：少 `Documentation=` 与 `SyslogIdentifier=`，
`ExecStart` 也不是同一个文件）。

```bash
systemctl daemon-reload
systemctl enable --now yuque-agent
journalctl -u yuque-agent -f          # 看日志
```

> `--no-sync` 是刻意的：让 `uv run` 直接用已经装好的 `.venv`，不去动它。
> 升级依赖时要手动 `uv sync` 一次。
>
> 用 `--journal` 会写回语雀《工作日志》。它**只在真有事发生时写**（静默轮询不写），
> 所以不会平白污染那篇文档。

## 5. 运维注意

**① 更新代码时 GitHub 可能连不上 —— 这是常态，不是偶发。**
实测：到 github.com 的 RTT 265ms、HTTPS 时好时坏，有过**连续 8 次重试全失败**
（`GnuTLS recv error (-110)`）过一阵又自己好。所以 `git pull` 失败是**预期之内**的。
`scripts/deploy.sh` 对此是**降级而不是假装**：取不到就打印警告、按当前 HEAD 继续。

实在要更新而又不通时，走**不经过 GitHub 的路**——从一台能连 GitHub 的机器直接 push 到生产机的检出：

```bash
# 生产机一次性设置：允许 push 到当前检出的分支并同步工作区
# （工作区不干净时 git 会拒绝，不会覆盖别人的改动）
ssh root@<地址> 'cd /opt/yuque-agent && git config receive.denyCurrentBranch updateInstead'

git push ssh://root@<地址>/opt/yuque-agent main       # 从开发机

# 再在生产机跑一次 deploy.sh（测试、对齐单元、重启、验收、记 ops.log 都照跑）
ssh root@<地址> 'sudo /opt/yuque-agent/scripts/deploy.sh'
```

这没绕过「部署只走上游 commit」：搬的还是 `main` 上同一条历史，只是换个搬运方式（`AGENTS.md` §1）。
**首次引导**：`deploy.sh` 自己也是靠一次 `git pull` 才到机器上的——全新部署先手工
`git pull --ff-only` 一次，之后都应走它。

**② 冷启动会跑一次归档会话（约 2 万 token）。** 全新部署时 `last_archive_title` 是空的，
`archive_due()` 立刻为真 → 第一次 tick 就跑一轮归档。不是 bug（顺带把知识库结构体检一遍），
但**别为了调试反复删 `state.json`**——每删一次就再烧一次。

**③ 日志走 journald（已设持久化）。** 否则默认只在内存里、重启就没——而这个项目的立场就是「留痕」。

**③.5 这台机器会自动做安全更新，会重启你的服务。** 实测 `unattended-upgrades` + `needrestart`
在凌晨升级库时顺手重启了 `yuque-agent` 与 `ssh`（反复几次）。`status=143` = 正常 SIGTERM，
不是崩溃；单元里必须写 `SuccessExitStatus=143`，否则日志里一串假故障，真出事时反而看不出来。
这类升级偶尔会**短暂断网**，日志里会出现一次 `YuqueError: 网络请求失败（ConnectError）`——
watcher 按设计吞掉并继续，下一轮就恢复。**看一次就行，不用管。**

**④ 真正的证据在 `runs/*/session.jsonl`**，不在 stdout：每次 run 的完整输入 / 输出 / 思考 /
工具参数都在那里，那是唯一事实来源。

**④.5 用一个「专属」的 LLM key，别复用你个人的。** `resolve_llm_key()` 的优先级是
`YQA_LLM_KEY` > `DEEPSEEK_API_KEY` > `~/.pi/agent/auth.json`——**最后那个 fallback 是个坑**：
服务器上若装了 pi（或把它的 `auth.json` 拷过去），而 `agent.env` 里变量名写错了，
程序会**静默**改用你的个人 key，两边用量混在一起算不清。所以 `agent.env` 里放**只给这台机器用的**
key，并且别在服务器上放 `~/.pi/agent/auth.json`。

`yqa doctor` 的「LLM key OK」**只表示变量存在**（过期 / 打错 / 额度用尽都显示 OK）；
`LLM 可用性`那一行才是真打了一发 API。要单独验 key 时（约 40 tokens）：

```bash
sudo -u yuque env -i HOME=/home/yuque PATH=/usr/local/bin:/usr/bin:/bin bash -c '
  set -a; . /home/yuque/.yuque/agent.env; set +a
  cd /opt/yuque-agent && uv run --no-sync python -c "
import sys; sys.path.insert(0, \"src\")
from yuque_agent import config
print(config.resolve_llm_key()[:8])      # 前 8 位对不对
"'
```

**⑤ 凭证权限**：`~/.yuque/*` 是 600。**600 只在 POSIX 上真生效**——Windows 上 `chmod`
改不动 ACL（「静默成功但什么都没改」）。

## 6. 手动调试

服务和手工命令用的**不是同一套环境**：systemd 会加载 `EnvironmentFile`，
手工 `sudo -u yuque ... yqa doctor` 不会——于是会看到「LLM key 未找到」这种**假警报**。
用这个包装脚本，它会把凭证带进子进程环境（且不进 `argv`，`ps` 看不到）：

```bash
cat > /usr/local/bin/yqa-as-service <<'WRAP'
#!/bin/bash
# 以服务身份（yuque 用户 + 它的凭证）手动跑一次 yqa
exec sudo -u yuque bash -c '
  set -a
  . /home/yuque/.yuque/agent.env
  set +a
  export HOME=/home/yuque
  # 和服务用**同一份**缓存：不设的话手动命令走 ~/.cache/uv，
  # 于是同一台机器上养出两份 uv 缓存，排查时先得想「哪份是新的」。
  export UV_CACHE_DIR=/var/lib/yuque-agent/.uv-cache
  exec /usr/local/bin/uv run --no-sync --directory /opt/yuque-agent yqa "$@"
' -- "$@"
WRAP
chmod 755 /usr/local/bin/yqa-as-service
```

```bash
yqa-as-service doctor                       # 自检（这是第一条该跑的）
yqa-as-service once --workspace /var/lib/yuque-agent/workspace   # 跑一轮
yqa-as-service sessions                     # 看本地留了哪些 run
yqa-as-service render <run_id>              # 把某次 run 渲染成人话
yqa-as-service reset-test-data --workspace /var/lib/yuque-agent/workspace --scope all --journal
```

> ⚠️ **任何会动工作区的命令都必须显式写 `--workspace /var/lib/yuque-agent/workspace`。**
> CLI 的默认值是相对路径 `workspace`，而这个脚本的工作目录是检出目录
> （`uv run --directory /opt/yuque-agent`）——不写就会落到 `<检出>/workspace`。
> 实测踩过（2026-09-25）：`reset-test-data` 因此把检出里一个陈旧的 `workspace/`
> 当成目标，**语雀里的申请文档照样被删了，本地产出却清了个空**。
> 现在这种工作区会被直接拒绝（要强跑加 `--force`）。

## 7. 上线后的验收清单

- [ ] `yqa-as-service doctor` 全绿（尤其 **语雀 token / LLM key / 知识库 / 写权限 / 时区**）
  - `LLM key` 那行**只说明变量存在**；末尾的 `LLM 可用性` 才是真打了一发 API。
    它显示失败时，直接看它写的原因（`key 无效` / `余额不足` / `网络不通` …）——
    那是**真的打了**，不是猜的。
  - 「申请清单」那行看得到当前周期与条数；「⚠ 清单隐私」出现时说明 `defaults` 非空（见 §10）。
- [ ] `systemctl is-enabled yuque-agent` 是 `enabled`（开机自启）
- [ ] `journalctl -u yuque-agent` 能看到「开始常驻：每 60s 轮询 …」
- [ ] 让一个真社员写一篇申请，**等 1~2 分钟**，确认：
  - `outbox/applications/` 出现申请 JSON
  - `outbox/notify/pending/` 出现对应的通知（受理必有 `accepted`）
  - 语雀《工作日志》多了一节，且**最新的在最上面**
- [ ] 语雀《指导文档（必读）》里描述的通知行为（「受理了会收到 QQ」）与实际情况一致
- [ ] 交付方（QQ 投递 / 教室借用插件）能读到 `outbox/` 并跑通一次

## 8. 已知需要下游配合的点

* **通知投递**：`outbox/notify/pending/` 需要有人来搬（QQ 桥是官方投递方，
  已拆成独立项目——见 §11）。没人搬就会一直堆着。协议见 `docs/handoff.md` §3。
* **申请消费**：`outbox/applications/` 里的 JSON 要交给负责提交教室的同学
  （`yqa export-plan` 能汇总成下游可直接吃的 `plan.json`）。协议见 `docs/handoff.md` §2。
* **借不到怎么办**：目前**没有任何通道**把「借失败了」回传。这个闭环要不要做、谁做，
  见 `docs/handoff.md` §4 的待确认项。

## 9. 申请清单的交付（outbox 的分区与取件）

产物目录**按申请周期分区**。周期翻转为周六 00:00，和语雀那边的归档同一时刻：

```
outbox/
├── applications/          活跃：当前周期
│   ├── <申请id>.json
│   └── index.json
├── plan.json              活跃交付件（下游 cac 就取这个文件）
├── plan.defaults.json     借用人信息（JYRXM / JYRDH …），**不随周期归档**
├── notify/                QQ 通知桥
└── archive/
    └── 0919-0925/         往期，和活跃期**完全同形**
        ├── plan.json      当周那个版本（冻结，交付凭证）
        └── applications/{*.json, index.json}
```

**为什么必须分区**（原来不分，是真机上的一个 bug）：`build_plan_json()` 扫的是
整个 `applications/` 目录，而没有任何代码会移走旧申请——于是周期翻转后，
上几周的申请仍然留在 `plan.json` 里，cac 照着提交就是**订一个已经过去的日期**。
当时看不出来，只因为知识库刚清空过。

**翻转由程序做**（`Runner.rotate_cycle_if_needed`），**不依赖 LLM 的归档会话**：
归档会话会失败（网络、模型、token 上限），而产物边界不能跟着它一起失败。
它每轮只做一次字符串比较，**零 token**。

**`plan.json` 每次写申请就重发**——下游拿不到推送，它只能看到「文件是不是新的」。
注意 `defaults`（借用人姓名/电话）会被落盘保存到 `plan.defaults.json`，
否则那次重发就把它们丢了。

### ⚠️ 上线前必须做：把「人名 → QQ」的映射配上（在**桥**那边）

通知发给谁，靠的是**桥的映射表**（工作区 `qqbot.json` 的 `notify.members`；
这个文件归桥自己的仓库维护）。**不配的话任何通知都发不出去**——全都会堆进
`outbox/notify/unrouted/`，而 `outbox/` 看上去一切正常，社员却什么都收不到。

核心这边只认一个事实：通知记录里的 `target` 能直投（见 `docs/handoff.md` §3）；
没有 `target` 的由桥按 `member.name` 查表。**验收时去桥那边确认 `unrouted/` 没在涨。**

`plan_updated`（提醒 cac 去下载）发给人名 `YQA_PLAN_ADMIN`，它必须在同一张映射表里能查到：

```bash
printf 'YQA_PLAN_ADMIN=%s\n' "<管理员的语雀人名>" >> /home/yuque/.yuque/agent.env
chmod 600 /home/yuque/.yuque/agent.env && systemctl restart yuque-agent
```

### 取件通道（cac 用）

下游跑的是浏览器里的油猴脚本，**没法直连服务器**，所以取件只能靠人工。两条路：

```bash
# ① scp（零新增攻击面，推荐日常用）
scp lihe@<服务器地址>:/var/lib/yuque-agent/workspace/lqogh0_jsjysq/outbox/plan.json .
# 往期的：
scp "lihe@<服务器地址>:/var/lib/yuque-agent/workspace/lqogh0_jsjysq/outbox/archive/0919-0925/plan.json" .

# ② 网页（见 §10；**无密钥**，打开即下载）
#    http://<服务器地址>:8787/
```

两条都在 `docs/handoff.md` 里给下游写了。

> **每次取完请对一眼 `cycle` 字段**：它必须是本周的周期号。这是唯一能一眼看出
> 「我拿到的是不是上周那份」的标记——服务器不会替你验证这一点。

## 10. 下载口（`yqa serve-plan`）

**没有密钥，打开即下载。** cac 的要求：访问 download 立刻拿到 `plan.json`。

`deploy/yuque-agent-plan.service`（内容以仓库里那份为准）：

```bash
install -m 644 /opt/yuque-agent/deploy/yuque-agent-plan.service \
    /etc/systemd/system/yuque-agent-plan.service
systemctl daemon-reload && systemctl enable --now yuque-agent-plan
curl -s http://127.0.0.1:8787/healthz      # 应回 ok
```

端口用 `--port` 或 `YQA_PLAN_PORT`（默认 8787）。

### 放行端口（这一步只能在控制台做）

云厂商的安全组**默认不放行** 8787：

- 阿里云轻量应用服务器 → 防火墙 → 添加规则：TCP `8787`，源 `0.0.0.0/0`

放行后从**外网**验一次：`curl http://<公网IP>:8787/healthz`。

### 路由

```text
GET /                          网页：当前周期 / 条数 / 更新时间 + 下载按钮
GET /download                  直接下载 plan.json（cac 说的那条）
GET /plan.json                 同上（别名，方便 curl / 脚本）
GET /archive/<周期>/plan.json    往期清单（那个版本是冻结的）
GET /healthz                   给监控用，不泄任何内容
```

### 它是哪个级别的东西（**别把它当安全边界**）

**这是一个公开端点：任何知道地址的人都能拿到当前周期的申请清单。**

原来设计过密钥，后来去掉了 —— 项目负责人和 cac 的判断是：服务器没有域名、
只有明文 HTTP，密钥在 URL 里、在浏览器历史里、在截图里都会漏；与其维持一个
「看着有防护、实际拦不住人」的假象，不如干脆不做防护，把「访问即下载」
做成一个清楚的事实。申请里的**活动信息**不算机密，这一点负责人明确接受了。

去掉密钥之后**还剩**的防护（都有测试）：

1. **路径不可越狱** —— 只放行由程序自己算出的两类文件，URL 里的周期还要过格式
   校验。这条现在是主要防线：一个公开端点最怕的就是被人顺着路径爬出去。
2. **`plan.defaults.json` 永远取不到** —— 它就在隔壁，但里面是**借用人姓名与手机号**。
3. **请求进 journald**（`journalctl -u yuque-agent-plan`）—— 「谁什么时候取了什么」
   留个痕，省得事后猜。

### ⚠️ 唯一真正的风险面：`defaults`

`plan.json` 里内联了 `defaults`（`JYRXM` 借用人姓名 / `JYRDH` 手机号 …），
而**下载口是公开的**。所以一旦有人往 `plan.defaults.json` 里填了真名和手机号，
它们就会跟着公开。

今天它是空的（**只有活动信息**），所以没事。但这件事不能等到哪天有人填了
才被偶然发现，所以：

- `yqa serve-plan` 启动时会检查，非空就打印告警；
- `yqa doctor` 里也有一行；
- 真要填的话，先确认这些字段可以公开。

> 反过来：**别**把 `plan.defaults.json` 加进下载口的路由。它比申请清单敏感得多，
> 而且 cac 不需要它——`defaults` 已经内联在 `plan.json` 里了。

## 11. QQ 桥（独立项目）

QQ 桥（通知投递 + QQ 命令入口）自 2026-09-24 起是**独立项目**：
代码、systemd 单元、部署脚本、维护文档都在它自己的仓库里。
拆分材料与交接说明见交接分支 `qqbot/handover-20260924` 的 `HANDOVER.md`。

它按 `docs/interface.md` 与核心交互，**不 import 核心内部、不轮询**：

| 方向 | 接口 |
|---|---|
| 核心 → 桥 | `outbox/notify/pending/*.json`（含可选的 `target` 直投字段） |
| 桥 → 核心 | `control/requests/*.json`（`once` / `archive` / `apply`），回执在 `control/done/` |
| 桥只读 | `state.json`、`runs/*/result.json`、`outbox/applications/index.json`、`plan.json` |

本仓库**不再提供** `yqa qq …` 命令，`deploy.sh` 也不管它的单元。
生产机上它是与核心并排的另一个检出（例如 `/opt/qq-bridge`），
两个进程**永远不许**同时做同一件事（两个轮询者 / 两个投递者）。
