#!/usr/bin/env bash
# 把 main 同步到生产检出，再交给 deploy.sh 部署 —— **开发机侧**的脚本。
#
# 为什么需要它：生产机到 GitHub 时通时断（docs/deploy.md §5①）。正常该由服务器自己
# fetch（deploy.sh 会做），但取不到时 deploy.sh 会「按当前 HEAD 继续」——那等于
# **悄悄部署了旧 commit**。所以这里先把要部署的 commit 送到生产检出，再调 deploy.sh
# （测试、对齐单元、重启、验收、记 ops.log 全都照跑，一条不少）。
#
# 服务器地址与账号**不进仓库**（docs/deploy.md 开头那条），只从参数或环境变量传。
#
# 用法：
#   scripts/sync-server.sh --server root@<地址>              # 同步并部署
#   scripts/sync-server.sh --server root@<地址> --dry-run     # 只看它打算做什么
#   YQA_SERVER=root@<地址> scripts/sync-server.sh
#
#   --branch <名字>     默认 main
#   --allow-unpushed    这个 commit 还没推上游也硬上（默认拒绝，见 AGENTS.md §1）
#   --dry-run           只做只读探测，不 push、不部署
set -euo pipefail

BRANCH=main
REPO=/opt/yuque-agent
SERVER="${YQA_SERVER:-}"
DRY_RUN=no
ALLOW_UNPUSHED=no

die() { printf '✗ %s\n' "$*" >&2; exit 1; }
say() { printf '\n== %s\n' "$*"; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="${2:?--server 后面要跟 user@host}"; shift 2 ;;
    --branch) BRANCH="${2:?--branch 后面要跟分支名}"; shift 2 ;;
    --dry-run) DRY_RUN=yes; shift ;;
    --allow-unpushed) ALLOW_UNPUSHED=yes; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "不认识的参数：$1（--help 看用法）" ;;
  esac
done

[ -n "$SERVER" ] || die "必须给 --server（或设 YQA_SERVER）——地址不进仓库，见 docs/deploy.md 开头"
[ -d .git ] || die "请在仓库根目录运行"

# 只在开发机上跑：生产机的检出不该把代码往自己身上推
case "$(pwd -P)" in
  "$REPO"|"$REPO"/*) die "这是生产机的检出——本脚本在开发机上跑（生产机更新走 deploy.sh）" ;;
esac

say "预检 1/3：工作区必须干净（跟踪的文件）"
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  printf '%s\n' "$DIRTY" >&2
  die "有未提交的改动：先提交。要部署的是「某个 commit」，不是一堆未提交的文件。"
fi

LOCAL_SHA="$(git rev-parse HEAD)"
LOCAL_SUBJECT="$(git log -1 --pretty=%s)"
printf '本地 %s = %s %s\n' "$BRANCH" "${LOCAL_SHA:0:7}" "$LOCAL_SUBJECT"

say "预检 2/3：这个 commit 在上游可见吗"
# 只看本地 remote-tracking ref（不联网；GitHub 在这台机器上也时通时断）
if git merge-base --is-ancestor HEAD "origin/$BRANCH" 2>/dev/null; then
  printf 'origin/%s 已包含它 ✓\n' "$BRANCH"
elif [ "$ALLOW_UNPUSHED" = "yes" ]; then
  printf '⚠ 本地看到的 origin/%s 不含这个 commit（还没推，或 remote-tracking 过期）——按 --allow-unpushed 继续\n' "$BRANCH"
else
  die "它还没到上游 origin/$BRANCH。生产机只该跑上游有的 commit（AGENTS.md §1）：
     先 git push origin $BRANCH（GitHub 不通就挂上你的代理）；
     实在推不动又必须上线，再加 --allow-unpushed 自己担这个责任。"
fi

say "预检 3/3：生产机现在在哪个 commit"
REMOTE_HEAD="$(ssh "$SERVER" "sudo git -C $REPO rev-parse HEAD")" || die "ssh 取不到生产机的 HEAD"
printf '生产机 HEAD = %s\n' "${REMOTE_HEAD:0:7}"

NEED_PUSH=no
if [ "$REMOTE_HEAD" = "$LOCAL_SHA" ]; then
  echo "已经在目标 commit 上了，不需要同步"
else
  say "先请生产机自己 fetch（干净路线）"
  if ssh "$SERVER" "sudo timeout 45 git -C $REPO fetch origin" >/dev/null 2>&1; then
    UPSTREAM_SHA="$(ssh "$SERVER" "sudo git -C $REPO rev-parse origin/$BRANCH" 2>/dev/null || true)"
    if [ "$UPSTREAM_SHA" = "$LOCAL_SHA" ]; then
      echo "生产机取到 GitHub 了 ✓（deploy.sh 会自己快进）"
    elif [ -n "$UPSTREAM_SHA" ]; then
      die "生产机取到的 origin/$BRANCH = ${UPSTREAM_SHA:0:7}，不是本地这个 commit。
     说明上游有别的提交，或者你推的不是这个分支。先对齐再来。"
    else
      NEED_PUSH=yes
    fi
  else
    echo "⚠ 生产机取不到 GitHub（这是常态）——改走「从本机 push 到生产检出」"
    NEED_PUSH=yes
  fi
fi

if [ "$NEED_PUSH" = "yes" ]; then
  say "从本机 push $BRANCH → 生产检出"
  if [ "$DRY_RUN" = "yes" ]; then
    echo "[dry-run] git push ssh://$SERVER$REPO $BRANCH"
  else
    git push "ssh://$SERVER$REPO" "$BRANCH"
    AFTER="$(ssh "$SERVER" "sudo git -C $REPO rev-parse HEAD")"
    [ "$AFTER" = "$LOCAL_SHA" ] || die "push 之后生产机 HEAD = ${AFTER:0:7}，不是 ${LOCAL_SHA:0:7}"
    printf '生产机 HEAD 已是 %s ✓\n' "${LOCAL_SHA:0:7}"
  fi
fi

say "交给 deploy.sh（取锁 / 测试 / 对齐单元 / 重启 / 验收 / 记 ops.log）"
if [ "$DRY_RUN" = "yes" ]; then
  echo "[dry-run] ssh $SERVER 'sudo $REPO/scripts/deploy.sh'"
  say "[dry-run] 到此为止，什么都没改"
  exit 0
fi
ssh "$SERVER" "sudo $REPO/scripts/deploy.sh"

say "✓ 完成：生产机跑 ${LOCAL_SHA:0:7} $LOCAL_SUBJECT"
