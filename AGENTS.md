# AGENTS.md —— 在这台生产机上干活的规矩（给代理，也给人）

> 这不是风格偏好：**每一条都对应一次真实事故**，时间、现象、证据都写在里面。
> 任何代理（或人）在动这个仓库 / 这台机器之前，先读完本文件，再读 [`docs/deploy.md`](docs/deploy.md)。

## 0. 先读这三份

| 文件 | 它是什么 |
|---|---|
| [`docs/deploy.md`](docs/deploy.md) | 部署真相：目录布局、三个 systemd 单元、二选一矩阵、验收清单 |
| [`docs/handoff.md`](docs/handoff.md) | 对下游（cac / 洋芋）的**冻结契约**：申请 JSON 与通知事件 |
| `AGENTS.md`（本文件） | 干活规矩：怎么改、怎么部署、什么绝对不许做 |

## 1. 一条上游，feature 分支交付

* **上游 = `Aalas1111/nju-yuque-agent`**。生产机 `/opt/yuque-agent` 的 `origin` 就是它，
  systemd 单元里的 `Documentation=` 也指向它。
* `OJuisPlease/nju-yuque-agent` 是**镜像**：只做同步，不要在它的 `main` 上直接堆自己的提交。
* 干活顺序：`git fetch` → 从 `origin/main` 开 feature 分支 → 提交 → 推到自己的 fork →
  请上游合并。上游是用 `Merge branch '…'` 收外部贡献的，别要求它 rebase。
* **服务器上永远不许 rebase / 手工 merge**：`/opt/yuque-agent` 只允许快进（见 §2.3）。
* 收工前**跟踪的文件**必须干净（`git status --porcelain --untracked-files=no` 为空）：
  改了东西又不提交，下一个人（或下一个代理）分不清哪些是有意的改动。
  未跟踪文件（`.env`、草稿、临时产物）不用提交，但要收拾——`scripts/deploy.sh` 会列出来提醒。

## 2. 生产机上的铁律

### 2.1 不许在生产机上跑测试（2026-09-23 事故）

* **事故**：在服务器上跑 `pytest`，`tests/test_qq_cli.py::test_logout_without_credentials`
  里的 `yqa qq logout --yes` **真的执行了**，把 `/home/yuque/.yuque/qqbot.json`
  连同备份一起删掉。凭证是扫码换来的——删一次就要重扫一次码。
  当天发生了**两次**（21:10 与 21:47），两次都让服务报「还没有绑定机器人」。
* 根因两条，都已修：`tests/conftest.py` 把 `$HOME` / `USERPROFILE` 关进临时目录；
  `credentials.py` 的默认凭证路径从「导入时求值的模块常量」改成惰性函数。
* **但闸门不是许可证**：
  * 测试优先在本地跑；必须在机器上跑时，用 `scripts/deploy.sh`（它显式把 `HOME` 指向临时目录）。
  * 新写的测试**不许**假设「默认路径下没有东西」，更不许真的调用 `logout`：
    要覆盖「没有凭证」就用 `tmp_path` 显式指定路径。

### 2.2 常驻进程必须是仓库里的 systemd 单元

* 只有三个单元，权威副本在 `deploy/`，生效位置是 `/etc/systemd/system/`：
  * `yuque-agent.service` —— 裸轮询（不带 QQ）
  * `yuque-agent-qq.service` —— 轮询 + 通知泵 + QQ 网关 + 运行中播报
  * `yuque-agent-plan.service` —— 带密钥的申请清单下载口（`yqa serve-plan`）
* **前两个二选一**：都轮询同一个工作区、都写 `state.json`，同时跑会互相覆盖
  （轻则重复通知，重则快照回退）。单元里用 `Conflicts=` 把它变成机制，不靠人记。
  本机启用的是 `yuque-agent-qq.service`。
* **不许** `nohup` / `setsid` / `&` 起常驻进程。实测踩过：有人手工起了 `yqa qq serve`，
  和 systemd 里那个抢同一个工作区。
* 改单元 = 改 `deploy/*.service` → `install` 到 `/etc` → `daemon-reload` →
  **同步 `docs/deploy.md` 里那份**。`tests/test_deploy_doc.py` 会拦住两边漂移。
* 新服务默认绑 `127.0.0.1`；要绑 `0.0.0.0` 必须在 `docs/deploy.md` 里写清楚它靠什么鉴权。

### 2.3 一次只能有一个写者

* 所有对生产机的改动走 **`scripts/deploy.sh`**：它取 `flock` 锁、只允许快进、
  在沙箱 `HOME` 里跑测试、把单元与仓库对齐、重启并验收，最后把
  「谁 / 什么时候 / 哪个 commit」追加进 `/var/lib/yuque-agent/ops.log`。
* 需要人肉操作时，先去 `ops.log` 记一笔「我要做什么、大概多久」，
  别让另一个代理在同一分钟里跟你抢同一个工作区。

## 3. 凭证（删一次 = 重扫一次码）

凭证有**三处**，都不许手删手改：

| 位置 | 作用 |
|---|---|
| `~/.yuque/qqbot.json` | 扫码登录落点（600，`yuque:yuque`） |
| `~/.yuque/qqbot.json.bak` | 每次写入同步的备份；主文件丢失/损坏时**自动恢复** |
| `~/.yuque/agent.env` 的 `YQA_QQ_APPID` / `YQA_QQ_SECRET` | systemd `EnvironmentFile`，**优先级最高**——文件被删也不掉线 |

* 换 / 清凭证只走 `yqa qq login` / `yqa qq logout`；`CredentialStore` 会自己维护备份。
* 密钥不落日志、不贴聊天、不进提交。要验证密钥是否可用，用
  `bots.qq.com/app/getAppAccessToken` 打一发，别把密钥打出来。
* 真丢了先别叫人扫码：先看 `.bak` → `agent.env` → root 的 `/root/.qq-recovered.json`。

## 4. 提交与身份

* 作者 = 真正写这段代码的人；提交者 = 把它放进仓库的人/代理。代理提交时写明是代理执行。
* 提交信息第一行说清「改了什么」，正文说「为什么 + 依据」（现象 / 日志 / 测试结果）。
* 大改动先跟人确认；别顺手重构别人的文件——那属于另一个人的工作，会制造看不见的冲突。

## 5. 交付前自检

1. `ruff check . && ruff format --check .`
2. `pytest`（本地；在机器上则必须经由 `scripts/deploy.sh`）
3. 碰了文档里的 systemd 单元 → `pytest tests/test_deploy_doc.py`
4. 部署后：三个单元 `systemctl is-active`，日志里有 `READY`、没有 `Traceback`，
   并且 `ops.log` 多了一行

## 6. 出事了怎么办

1. **先留证据再动手**：`journalctl -u <unit>`、`/var/log/auth.log`、`git log`、
   `stat -c '%y %n' <文件>`（文件被谁何时删的，mtime 常常是唯一线索）。
2. 凭证类故障按 §3 的顺序找备份，别急着重启（重启会把内存里那份也弄丢）。
3. 结论写进提交信息或 `docs/`，**不要只留在聊天里**——下一个代理看不到聊天。
