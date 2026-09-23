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
| 出网 | `api.yuque.com`、`api.deepseek.com`（要 QQ 再加 `bots.qq.com`、`api.sgroup.qq.com`） | 还要能拉代码（见 §5 的注意） |
| 入网端口 | **不需要开公网端口**（QQ 扫码登录页会临时开本地端口，登完即关） | |
| 常驻能力 | 要能**长期挂进程**（systemd / supervisor）。**不能用 serverless / 函数计算** | 归档是时钟驱动的，进程不能被回收 |
| 时钟 | NTP 正常 | 「每周六 00:00 归档」靠它 |
| GPU | 不需要 | LLM 走 DeepSeek 的 API |

> **不需要 ICP 备案**——前提是**别绑域名**。用 `公网IP:端口` 访问即可。
> 一旦用域名对外提供 web 服务，境内服务器就要备案（几周）。

## 2. 目录布局

```
/opt/yuque-agent/                    代码：git clone（root 所有，服务只读）
├── src/yuque_agent/                 17 个模块 + qqbot/ 子包
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

## 4. systemd 单元

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

下面是它的内容（给人读的，**改动请改仓库里那个文件**——
`tests/test_deploy_doc.py` 会断言这两边逐行一致，不一致就测试失败）：

```ini
[Unit]
Description=yuque-agent — 让 LLM 接管语雀知识库（程序只做感知与留痕）
Documentation=https://github.com/Aalas1111/nju-yuque-agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=yuque
Group=yuque
WorkingDirectory=/opt/yuque-agent
EnvironmentFile=/home/yuque/.yuque/agent.env
Environment=HOME=/home/yuque
Environment=PYTHONUNBUFFERED=1
Environment=UV_CACHE_DIR=/var/lib/yuque-agent/.uv-cache
SyslogIdentifier=yuque-agent

ExecStart=/usr/local/bin/uv run --no-sync yqa run --workspace /var/lib/yuque-agent/workspace --interval 60 --quiet-seconds 45 --journal

Restart=always
RestartSec=15

# `systemctl stop` 时 Python 以 143（128+SIGTERM）退出，systemd 默认把它记成
# 「Failed with result 'exit-code'」。那是**正常停止**，不是故障——不声明的话，
# 每次重启 / 自动更新都会在日志里留一串假 "Failed"，把真故障淹掉。
# （这个项目的整个立场就是「日志得能信」，所以不能有这种噪音。）
SuccessExitStatus=143

# 加固：这个进程不需要新特权、不需要改系统
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

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

**① 更新代码时 GitHub 可能连不上。**
实测从境内服务器 `git pull` 会间歇性失败（`GnuTLS recv error` / 连不上 443，
最长卡了两分多钟）。更新时请重试，或者改用 `rsync`/`scp` 从开发机推。

**② 冷启动会跑一次归档会话（约 2 万 token）。**
全新部署时 `last_archive_title` 是空的，于是 `archive_due()` 立刻为真 →
第一次 tick 就跑一轮归档。这不是 bug（它顺带把知识库结构体检一遍），
但**要知道冷启动那一下是有成本的**。跑完水位线就落上了。
（所以别为了调试反复删 `state.json`。）

**③ 日志走 journald，已设持久化。**
否则默认只存在内存里、重启就没了——而这个项目的整个立场就是「留痕」。

**③.5 这台机器会自动做安全更新，会重启你的服务。**
实测：`unattended-upgrades` + `needrestart` 在凌晨升级了库，顺手把
`yuque-agent` 和 `ssh` 都重启了（06:47~06:59 之间反复几次，因为同时升级了多个包）。
`status=143` = 正常 SIGTERM，不是崩溃。所以单元里要写：

```ini
SuccessExitStatus=143
```

不写的话每次正常停止都会被记成 `Failed with result 'exit-code'`，
日志里一串假故障——真出事的时候反而看不出来。

另外这类升级偶尔会**短暂断网**（systemd-resolved 被重启），
实测在日志里看到过一次 `YuqueError: 网络请求失败（ConnectError）`——
watcher 按设计吞掉并继续了，下一轮就恢复。**这类错误看一次就行，不用管。**

**④ 真正的证据在 `runs/*/session.jsonl`**，不在 stdout。每次 run 的完整
LLM 输入/输出/思考/工具参数都在那里，那是唯一事实来源。

**④.5 用一个「专属」的 LLM key，别复用你个人的。**

`config.resolve_llm_key()` 的优先级是
`YQA_LLM_KEY` > `DEEPSEEK_API_KEY` > `~/.pi/agent/auth.json`。

**最后那个 fallback 是个坑**：如果你在服务器上装了 pi（或把它的 `auth.json` 拷过去），
而 `agent.env` 里的变量名写错了，程序会**静默**改用 pi 的个人 key——
于是你自己的用量和这台服务器的用量混在一起，两边都算不清。
所以：`agent.env` 里放**只给这台机器用的** key，并且别在服务器上放
`~/.pi/agent/auth.json`。

`yqa doctor` 的「LLM key OK」**只表示那个变量存在**——key 过期、打错、
额度用尽，它都照样显示 OK。想真验一次就这样（约 40 tokens）：

```bash
sudo -u yuque env -i HOME=/home/yuque PATH=/usr/local/bin:/usr/bin:/bin bash -c '
  set -a; . /home/yuque/.yuque/agent.env; set +a
  cd /opt/yuque-agent && uv run --no-sync python -c "
import sys; sys.path.insert(0, \"src\")
from yuque_agent import config
print(config.resolve_llm_key()[:8])      # 前 8 位对不对
"'
```

**⑤ 凭证权限**：`~/.yuque/*` 是 600。**注意 600 只在 POSIX 上真生效**——
Windows 上 `chmod` 改不动 ACL，会「静默成功但什么都没改」（详见 `docs/qqbot.md` §6）。

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
```

## 7. 上线后的验收清单

- [ ] `yqa-as-service doctor` 全绿（尤其 **语雀 token / LLM key / 知识库 / 写权限 / 时区**）
  - 注意 `LLM key` 那行**只说明变量存在**；末尾的 `LLM 可用性` 才是真打了一发 API。
    它显示失败时，直接看它写的原因（`key 无效` / `余额不足` / `网络不通` …）——
    那是**真的打了**，不是猜的。
- [ ] `systemctl is-enabled yuque-agent` 是 `enabled`（开机自启）
- [ ] `journalctl -u yuque-agent` 能看到「开始常驻：每 60s 轮询 …」
- [ ] 让一个真社员写一篇申请，**等 1~2 分钟**，确认：
  - `outbox/applications/` 出现申请 JSON
  - `outbox/notify/pending/` 出现对应的通知（受理必有 `accepted`）
  - 语雀《工作日志》多了一节，且**最新的在最上面**
- [ ] 语雀《指导文档（必读）》里描述的通知行为（「受理了会收到 QQ」）与实际情况一致
- [ ] 交付方（QQ 投递 / 教室借用插件）能读到 `outbox/` 并跑通一次

## 8. 已知需要下游配合的点

* **通知投递**：`outbox/notify/pending/` 需要有人来搬（`yqa qq notify`，或投递方自己实现）。
  没人搬就会一直堆着。协议见 `docs/handoff.md` §3。
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

### 取件通道（cac 用）

下游跑的是浏览器里的油猴脚本，**没法直连服务器**，所以取件只能靠人工。两条路：

```bash
# ① scp（零新增攻击面，推荐日常用）
scp lihe@<服务器地址>:/var/lib/yuque-agent/workspace/lqogh0_jsjysq/outbox/plan.json .
# 往期的：
scp "lihe@<服务器地址>:/var/lib/yuque-agent/workspace/lqogh0_jsjysq/outbox/archive/0919-0925/plan.json" .

# ② 网页（见 §10；输密钥下载）
#    http://<服务器地址>:8787/
```

两条都在 `docs/handoff.md` 里给下游写了。

> **每次取完请对一眼 `cycle` 字段**：它必须是本周的周期号。这是唯一能一眼看出
> 「我拿到的是不是上周那份」的标记——服务器不会替你验证这一点。

## 10. 下载口（`yqa serve-plan`）

`deploy/yuque-agent-plan.service`：

```ini
[Unit]
Description=yuque-agent 申请清单下载口（带密钥的 HTTP，给 cac 手动取件）
Documentation=https://github.com/Aalas1111/nju-yuque-agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=yuque
Group=yuque
WorkingDirectory=/opt/yuque-agent
EnvironmentFile=/home/yuque/.yuque/agent.env
Environment=HOME=/home/yuque
Environment=PYTHONUNBUFFERED=1
Environment=UV_CACHE_DIR=/var/lib/yuque-agent/.uv-cache
SyslogIdentifier=yuque-agent-plan

ExecStart=/usr/local/bin/uv run --no-sync yqa serve-plan --workspace /var/lib/yuque-agent/workspace --port 8787

Restart=always
RestartSec=15

# systemctl stop 时 Python 以 143（SIGTERM）退出，那是正常停止，不是故障。
SuccessExitStatus=143

# 加固：这个进程只需要读产物 + 对外监听，不需要新特权、不需要改系统。
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

### 装它

```bash
# ① 配密钥（**必须**，否则服务拒绝启动——失败关闭）
printf 'YQA_PLAN_KEY=%s\n' "$(openssl rand -hex 16)" >> /home/yuque/.yuque/agent.env
# 另外可选：指定管理员人名（见 docs/handoff.md §3.3 的 plan_updated）
printf 'YQA_PLAN_ADMIN=%s\n' "<管理员的语雀人名>" >> /home/yuque/.yuque/agent.env
chmod 600 /home/yuque/.yuque/agent.env

# ② 装单元并起服务
install -m 644 /opt/yuque-agent/deploy/yuque-agent-plan.service \
    /etc/systemd/system/yuque-agent-plan.service
systemctl daemon-reload && systemctl enable --now yuque-agent-plan
curl -s http://127.0.0.1:8787/healthz      # 应回 ok
```

### 放行端口

云厂商的安全组**默认不放行** 8787，这一步只能在控制台做：

- 阿里云轻量应用服务器 → 防火墙 → 添加规则：TCP `8787`，源 `0.0.0.0/0`
- 服务器本机一般不用再动（没有 ufw/iptables 规则）

放行后从**外网**验一次：`curl http://<公网IP>:8787/healthz`。

### 它是哪个级别的东西（说清楚，免得被当成安全边界）

服务器**没有域名**，所以只有明文 HTTP——**密钥和申请内容都在网上裸奔**。
项目负责人明确接受了这个风险（活动信息不算机密信息）。

所以它做不到「防住有心人」，能做到的是三件（都有测试）：

1. **失败关闭**：没配 `YQA_PLAN_KEY` 就拒绝启动，不会出现「没密钥也能下」；
2. **路径不可越狱**：只放行 `outbox/plan.json` 和 `outbox/archive/<周期>/plan.json`
   那两类由程序自己算出来的文件，URL 里的周期还要过格式校验。
   特别地 `plan.defaults.json`（**借用人姓名与电话**）就在隔壁，取不到；
3. **尝试可见**：请求进 `journalctl -u yuque-agent-plan`，**但不记密钥**；
   同一来源失败超限锁 5 分钟（不影响别的来源）。

> 别把 `plan.defaults.json` 放进下载口。它比申请清单敏感得多（真名 + 手机号），
> 而且 cac 不需要它——`defaults` 已经内联在 `plan.json` 里了。
