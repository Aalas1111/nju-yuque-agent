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
/opt/yuque-agent           代码（root 所有，服务只读）
/var/lib/yuque-agent      运行状态：workspace/（yuque 用户所有）
/home/yuque/.yuque/        凭证（600，yuque 用户所有）
  ├── auth.json            {"token": "<语雀写权限令牌>"}
  └── agent.env            DEEPSEEK_API_KEY=<key>
/etc/systemd/system/yuque-agent.service
/usr/local/bin/yqa-as-service   手动调试用的包装脚本（见 §6）
```

**为什么分这么细**：代码只读、状态可写、凭证最紧——服务不需要写自己的代码，
也不需要碰别人的东西。即使这个进程被攻破，能改的也只限于它自己的状态目录。

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

`/etc/systemd/system/yuque-agent.service`：

```ini
[Unit]
Description=yuque-agent — 让 LLM 接管语雀知识库（程序只做感知与留痕）
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

ExecStart=/usr/local/bin/uv run --no-sync yqa run \
    --workspace /var/lib/yuque-agent/workspace \
    --interval 60 --quiet-seconds 45 --journal

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
