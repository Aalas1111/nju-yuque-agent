"""QQ 开放平台的**本地替身** + 扫码绑定/投递全链路自测（不联外网）。

开发期用它验证整条链路：

```
[stub q.qq.com] create_bind_task → 出示二维码 → poll_bind_result(PENDING×N → COMPLETED)
        → 用本地 key AES-256-GCM 加密 AppSecret → 本地解密 → 凭证落盘
        → 造一条通知事件（走真实的 outputs.write_notice）
        → NotifyBridge 扫 pending/ → [stub api.sgroup.qq.com] 发消息 → 移进 done/
```

```bash
uv run python scripts/qqbot_sim.py                 # 跑完整链路并打印每一步
uv run python scripts/qqbot_sim.py --qr            # 顺便在终端画出二维码
uv run python scripts/qqbot_sim.py --polls 5       # 模拟「用户过一会儿才扫」
uv run python scripts/qqbot_sim.py --keep          # 保留中间产物（默认跑完删掉）
```

为什么值得单独写一个：真实扫码需要人拿手机、真实投递需要绑定好的机器人，
而**这条链路里最容易错的是协议细节**（key 的用法、密文格式、seq 顺序、文件移动），
本地替身能让这些细节每次改动都跑一遍。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

from yuque_agent.config import DEFAULT_REPO, Settings  # noqa: E402
from yuque_agent.outputs import write_notice  # noqa: E402
from yuque_agent.qqbot.bridge import NotifyBridge  # noqa: E402
from yuque_agent.qqbot.client import QQBotClient  # noqa: E402
from yuque_agent.qqbot.config import NotifyTarget, QQBotConfig  # noqa: E402
from yuque_agent.qqbot.credentials import CredentialStore, account_from_bind  # noqa: E402
from yuque_agent.qqbot.login import QrLoginFlow  # noqa: E402
from yuque_agent.qqbot.protocol import QQBotProtocol  # noqa: E402
from yuque_agent.qqbot.qr import has_qr_support, terminal_qr  # noqa: E402

DEFAULT_APP_ID = "102000001"
DEFAULT_APP_SECRET = "sim-app-secret-do-not-use"
DEFAULT_OPENID = "sim-user-openid"


class StubQQPlatform:
    """QQ 开放平台的本地替身（满足 :class:`~yuque_agent.qqbot.protocol.Transport`）。

    关键点：``bot_encrypt_secret`` 是**用它从 create_bind_task 请求里拿到的 key 加密的**，
    和真实平台的行为一致——所以这条链路真的在验证 AES-GCM 那一段，而不是塞了个假密文。
    """

    def __init__(
        self,
        *,
        app_id: str = DEFAULT_APP_ID,
        app_secret: str = DEFAULT_APP_SECRET,
        openid: str = DEFAULT_OPENID,
        polls_before_scan: int = 2,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.openid = openid
        self.polls_before_scan = polls_before_scan
        self.bind_key = ""
        self.polls = 0
        self.sent: list[dict[str, Any]] = []
        self.calls: list[str] = []

    # -- Transport 协议 ---------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        del timeout
        self.calls.append(f"{method} {url}")
        if url.endswith("/lite/create_bind_task"):
            self.bind_key = str((json or {}).get("key") or "")
            return httpx.Response(200, json={"retcode": 0, "data": {"task_id": "sim-task-1"}})
        if url.endswith("/lite/poll_bind_result"):
            return self._poll()
        if url.endswith("/app/getAppAccessToken"):
            return httpx.Response(
                200, json={"access_token": "sim-access-token", "expires_in": 7200}
            )
        if "/messages" in url:
            self.sent.append({"url": url, "body": json, "headers": dict(headers or {})})
            return httpx.Response(200, json={"id": f"sim-msg-{len(self.sent)}"})
        return httpx.Response(404, json={"message": f"stub 没实现：{method} {url}"})

    def close(self) -> None:
        return None

    # -- 内部 -------------------------------------------------------------
    def _poll(self) -> httpx.Response:
        self.polls += 1
        if self.polls <= self.polls_before_scan:
            return httpx.Response(200, json={"retcode": 0, "data": {"status": 1}})
        return httpx.Response(
            200,
            json={
                "retcode": 0,
                "data": {
                    "status": 2,
                    "bot_appid": self.app_id,
                    "bot_encrypt_secret": self.encrypt(self.app_secret),
                    "openid": self.openid,
                },
            },
        )

    def encrypt(self, plain: str) -> str:
        """完全照官方格式造密文：``base64(IV(12) + ciphertext + Tag(16))``。"""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = base64.b64decode(self.bind_key) if self.bind_key else b"0" * 32
        iv = os.urandom(12)
        sealed = AESGCM(key).encrypt(iv, plain.encode("utf-8"), None)
        return base64.b64encode(iv + sealed).decode("ascii")


def step(number: int, text: str) -> None:
    print(f"\n[{number}] {text}")


def main() -> int:
    parser = argparse.ArgumentParser(description="QQBot 接入本地自测（不联外网）")
    parser.add_argument(
        "--workspace", default="workspace/_qqbot_sim", help="自测工作区（默认临时目录）"
    )
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--polls", type=int, default=2, help="模拟扫之前先轮询几次（默认 2）")
    parser.add_argument("--qr", action="store_true", help="在终端画出二维码")
    parser.add_argument("--keep", action="store_true", help="保留中间产物（默认跑完删掉）")
    parser.add_argument("--member", default="张三", help="通知里那个语雀人名（用于测映射）")
    args = parser.parse_args()

    workspace = Path(args.workspace)
    if workspace.exists() and not args.keep:
        shutil.rmtree(workspace, ignore_errors=True)

    settings = Settings(repo=args.repo, workspace=workspace)
    settings.ensure_dirs()
    platform = StubQQPlatform(polls_before_scan=args.polls)
    protocol = QQBotProtocol(transport=platform)  # type: ignore[arg-type]
    failures: list[str] = []

    # ── 1. 扫码绑定 ─────────────────────────────────────────────────────
    step(1, "扫码绑定：create_bind_task → 出示二维码 → 轮询 → 解密 AppSecret")
    shown: list[str] = []

    def on_qr(url: str, attempt: int, _data_url: str | None) -> None:
        shown.append(url)
        print(f"    第 {attempt} 张二维码：{url}")
        if args.qr and has_qr_support():
            art = terminal_qr(url)
            if art:
                print(art)

    flow = QrLoginFlow(
        protocol,
        source="qqbot_sim",
        poll_interval=0.01,
        on_qr=on_qr,
        on_status=lambda text: print(f"    · {text}"),
    )
    result = flow.run()
    if not result.connected:
        print(f"    ✗ 绑定失败：{result.message}")
        return 1
    print(f"    ✓ 绑定成功：AppID={result.app_id} 刷新次数={result.refreshes}")
    if result.app_secret != DEFAULT_APP_SECRET:
        failures.append("解出来的 AppSecret 和替身里放的不一致（AES-GCM 链路有问题）")

    # ── 2. 凭证落盘 ─────────────────────────────────────────────────────
    step(2, "凭证落盘（600，原子写）")
    store = CredentialStore(settings.root / "qqbot.json")
    account = account_from_bind(
        app_id=result.app_id,
        app_secret=result.app_secret,
        user_openid=result.user_openid,
        source="qqbot_sim",
    )
    store.save(account)
    loaded = store.get("default")
    print(f"    ✓ {store.path}（{store.path.stat().st_mode & 0o777:o}）")
    print(f"      {loaded.describe() if loaded else '(读不出来)'}")
    if loaded is None or loaded.app_secret != DEFAULT_APP_SECRET:
        failures.append("落盘后读回来的凭证不对")

    # ── 3. 发一条通知（走真实的 outputs.write_notice）─────────────────────
    step(3, "造一条通知事件（真实契约：outbox/notify/pending/）")
    write_notice(
        settings,
        kind="rejected",
        payload={
            "doc": {"doc_id": 285808143, "title": "社团例会", "url": "https://nova.yuque.com/sim"},
            "member": {"name": args.member},
            "summary": "「社团例会」活动时间起止写反了",
            "message": "「社团例会」这份申请我没法提交：活动时间写的是 17:00-16:00，结束时间比开始时间还早，"
            "应该是写反了。改完保存就行，不用做别的动作。",
            "reasons": ["活动时间 17:00-16:00，结束早于开始"],
        },
    )
    pending = sorted(settings.notify_dir.glob("pending/*.json"))
    print(f"    ✓ pending 里有 {len(pending)} 条：{pending[0].name if pending else '(空)'}")

    # ── 4. 投递（真实 bridge + 真实 client，只把平台换成替身）─────────────
    step(4, "投递：扫描 pending/ → 按 seq 发送 → 移进 done/")
    client = QQBotClient(account, protocol=QQBotProtocol(transport=platform))  # type: ignore[arg-type]
    config = QQBotConfig(
        notify_default=NotifyTarget("group", "sim-group-openid"),
        members={args.member: NotifyTarget("c2c", "sim-user-openid")},
        inbound_allow=("sim-admin-openid",),
        inbound_admins=("sim-admin-openid",),
    )
    bridge = NotifyBridge(notify_dir=settings.notify_dir, sender=client, config=config)
    results = bridge.drain()
    for item in results:
        print(f"    · {item.describe()}")

    if not platform.sent:
        failures.append("没有发出任何消息")
    else:
        sent = platform.sent[0]
        print(f"    ✓ 平台收到 {len(platform.sent)} 条；第一条发往 {sent['url']}")
        print(f"      正文前 30 字：{str(sent['body'].get('content'))[:30]}…")
        if sent["url"] != "https://api.sgroup.qq.com/v2/users/sim-user-openid/messages":
            failures.append(f"发送路径不对：{sent['url']}")
        if sent["headers"].get("Authorization") != "QQBot sim-access-token":
            failures.append("Authorization 头不对")

    stats = bridge.stats()
    print(f"    ✓ 目录状态：{stats}")
    if stats["pending"] != 0 or stats["done"] != 1:
        failures.append(f"文件移动不对：{stats}")
    if bridge.audit_path.exists():
        last = json.loads(bridge.audit_path.read_text(encoding="utf-8").strip().splitlines()[-1])
        print(f"    ✓ 审计：{last['status']} → {last['target']}")

    # ── 5. 再投一次（验证幂等：done 里的不会被重发）───────────────────────
    step(5, "再投一次：pending 已空，不应该重复发送")
    again = bridge.drain()
    print(f"    ✓ 本轮结果：{len(again)} 条；平台累计收到 {len(platform.sent)} 条")
    if again or len(platform.sent) != 1:
        failures.append("重复投递了（幂等性被破坏）")

    client.close()
    protocol.close()

    # ── 收尾 ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 62)
    if failures:
        print("✗ 自测失败：")
        for item in failures:
            print(f"  · {item}")
    else:
        print("✓ 全链路通过：扫码绑定 → 解密 → 落盘 → 通知投递 → 幂等")
    if args.keep:
        print(f"  中间产物保留在 {workspace.resolve()}")
    else:
        shutil.rmtree(workspace, ignore_errors=True)
    print(f"  平台调用：{len(platform.calls)} 次")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
