#!/usr/bin/env bash
# 把生产机更新到上游 —— 这是**唯一**被允许的更新方式（见 AGENTS.md §2.3）。
#
# 它做的事，按顺序：
#   1. 取 flock（同一时刻只允许一个写者：人 / 代理 / 定时任务都算）
#   2. 确认工作区干净
#   3. 快进到 origin/main（服务器上禁止 rebase / 手工 merge）
#   4. 在**临时 HOME** 里跑测试（AGENTS.md §2.1：测试碰过生产凭证）
#   5. 把 deploy/*.service 与 /etc/systemd/system/ 对齐 + daemon-reload
#   6. 启用单元：轮询（唯一写 state.json 的那个）+ 下载口，重启，验收
#   7. 把「谁 / 什么时候 / 哪个 commit」追加进 /var/lib/yuque-agent/ops.log
#
# 用法（注意 `./`：sudo 的 PATH 里通常没有当前目录，写 `sudo scripts/deploy.sh`
# 会报 command not found —— 实测过）：
#   sudo ./scripts/deploy.sh            # 核心两个单元（QQ 桥是独立项目，见 docs/interface.md）
#
# 首次引导：这个脚本本身要先在机器上（它自己会 fetch，但得先有它）。
# 见 docs/deploy.md §5②。
#
# 任何一步失败就停下，不改任何东西 —— 部分部署比不部署更难查。

set -euo pipefail

REPO="${REPO:-/opt/yuque-agent}"
STATE="${STATE:-/var/lib/yuque-agent}"
LOG="$STATE/ops.log"
LOCK="$STATE/.deploy.lock"
WHO="${WHO:-$(whoami)@$(hostname -s)}"

UNITS=(yuque-agent.service yuque-agent-plan.service)
POLLING=yuque-agent.service
PLAN=yuque-agent-plan.service

say() { printf '\n== %s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "需要 root（要 install 单元、restart systemd）"

say "取部署锁（$LOCK）"
mkdir -p "$STATE"
exec 9>"$LOCK"
flock -n 9 || die "另一个部署正在进行（$LOCK 被占用）。等它结束再来，别抢同一个工作区。"

cd "$REPO"
[ -f deploy/yuque-agent.service ] || die "$REPO 看起来不是这个项目的检出"

say "工作区必须干净（跟踪的文件）"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no >&2
  die "有未提交的改动：先提交或 stash。部署只走「上游里有的 commit」。"
fi
UNTRACKED="$(git ls-files --others --exclude-standard)"
if [ -n "$UNTRACKED" ]; then
  # 未跟踪的常常是本地/机密文件（.env、草稿），不该逼人提交；但也不能装作没看见。
  printf '⚠ 有未跟踪文件（不影响本次部署，但请收拾）：\n%s\n' "$UNTRACKED"
fi

say "快进到上游"
if timeout 45 git fetch origin; then
  git merge --ff-only origin/main
else
  # docs/deploy.md §5①：这台机器到 GitHub 时通时不通。取不到就按当前 HEAD 部署，
  # 但要让人看见这件事，别假装同步过了。
  echo "⚠ 取不到 origin（网络问题？）—— 跳过快进，按当前 HEAD 继续"
fi
COMMIT="$(git rev-parse --short HEAD)"
SUBJECT="$(git log -1 --pretty=%s)"

say "测试（HOME 关进临时目录）"
SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
HOME="$SANDBOX" PYTHONPATH=src .venv/bin/python -m pytest -q

say "systemd 单元与仓库对齐"
install -m 644 "${UNITS[@]/#/deploy/}" /etc/systemd/system/
if [ -d /etc/systemd/system/yuque-agent.service.d ]; then
  echo "⚠ 还有 drop-in：$(ls /etc/systemd/system/yuque-agent.service.d)"
  echo "  老做法已废弃（它会覆盖 ExecStart，让单元跑别的命令）——确认后删掉"
fi
systemctl daemon-reload

say "启用单元（轮询 + 下载口）"
systemctl enable "$POLLING" >/dev/null
systemctl restart "$POLLING"
systemctl enable "$PLAN" >/dev/null
systemctl restart "$PLAN"
sleep 10

say "验收"
RC=0
CHECK=("$POLLING" "$PLAN")
for u in "${CHECK[@]}"; do
  state="$(systemctl is-active "$u")"
  printf '%-28s %s\n' "$u" "$state"
  [ "$state" = "active" ] || RC=1
done
if [ "$RC" != "0" ]; then
  echo "有单元没起来：journalctl -u <unit> -n 50 看原因" >&2
fi

printf '%s deploy %s %s %s\n' "$(date -Is)" "$WHO" "$COMMIT" "$SUBJECT" | tee -a "$LOG"
if [ "$RC" = "0" ]; then
  say "✓ 部署完成：$COMMIT $SUBJECT"
else
  say "✗ 部署不完整：$COMMIT $SUBJECT（上面的单元没全起来）"
fi
exit "$RC"
