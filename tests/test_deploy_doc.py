"""守卫：`deploy/*.service` 里那几件「一出事就难查」的指令必须还在。

**为什么不再对照 `docs/deploy.md`**：文档里抄一份单元内容，下场是**文档自己先漂**
（实测：少了 `Documentation=` 与 `SyslogIdentifier=`，`ExecStart` 也不是同一个文件，
而这种事没人会去对账）。现在权威副本只有 `deploy/*.service` 这一份，文档只指向它。

但「文件在那儿」不等于「内容对」：把 `SuccessExitStatus=143` 删掉，文件照样是合法单元。
所以这里把真出过事、一出事就难查的指令单独钉死。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 仓库里允许存在的单元。多一个都要先说清楚——见 test_qq_unit_is_gone_from_this_repo。
UNITS = ("yuque-agent.service", "yuque-agent-plan.service")

#: 轮询只有一个写者：`yuque-agent.service` 是**唯一**允许轮询的单元。
POLLING_UNIT = "yuque-agent.service"


def _unit(name: str) -> str:
    return (ROOT / "deploy" / name).read_text(encoding="utf-8")


# -- 结构性 ---------------------------------------------------------------


def test_every_unit_file_exists_and_has_the_three_sections() -> None:
    for name in UNITS:
        path = ROOT / "deploy" / name
        assert path.exists(), f"deploy/{name} 不见了"
        body = path.read_text(encoding="utf-8")
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in body, f"{name} 里少了 {section}"


# -- 关键指令（挡住「顺手改坏了」）-----------------------------------------


def test_agent_unit_pins_the_things_that_actually_matter() -> None:
    """把关键指令单独钉一遍。"""
    body = _unit("yuque-agent.service")
    must_have = {
        # 时区靠程序自己钉死，但 HOME 得对，否则 uv / 凭证找不到
        "Environment=HOME=/home/yuque": "服务账号的家目录（凭证在它下面）",
        "EnvironmentFile=/home/yuque/.yuque/agent.env": "LLM key 从这儿进来",
        # 不加这个，每次正常停止都会在日志里留一串假 Failed（真故障被淹掉）
        "SuccessExitStatus=143": "SIGTERM 是正常停止，不是故障",
        # 加固：这个进程不需要新特权、也不该写自己的代码
        "NoNewPrivileges=true": "加固",
        "ProtectSystem=full": "加固",
        "User=yuque": "用专用用户跑，不用 root",
    }
    missing = [f"{k}（{why}）" for k, why in must_have.items() if k not in body]
    assert not missing, "单元里少了关键指令：\n  " + "\n  ".join(missing)


def test_plan_service_also_runs_unprivileged_and_has_no_baked_key() -> None:
    """下载口是整个项目唯一对外的口，这几件不能松。"""
    body = _unit("yuque-agent-plan.service")
    assert "User=yuque" in body
    # 密钥必须走 EnvironmentFile（600 的那个），不能写进单元
    # —— 单元是 644，世界可读。
    assert "EnvironmentFile=/home/yuque/.yuque/agent.env" in body
    assert "YQA_PLAN_KEY" not in body, "密钥不能写进单元文件（单元 644，公开可读）"
    assert "SuccessExitStatus=143" in body
    assert "ProtectSystem=full" in body
    # 端口与工作区必须显式写出来：默认值是给本机开发用的相对路径
    assert "/var/lib/yuque-agent/workspace" in body


def test_unit_never_runs_as_root() -> None:
    for name in UNITS:
        assert "User=root" not in _unit(name), f"{name} 不该用 root 跑"


# -- AGENTS.md 跟着走 -----------------------------------------------------

AGENTS = ROOT / "AGENTS.md"


def test_agents_md_lists_every_unit() -> None:
    """`AGENTS.md` 必须提到 `deploy/` 下的**每个**单元。

    为什么：它是代理/人进仓库前「先读的那几份」之一，里面写着「只有这两个单元」。
    部署里新增一个单元而忘了改它，下一个人就会按旧清单去理解生产机。

    这条守卫是拿一次真漂移换来的：`AGENTS.md` 当时把下载口写成「带密钥的」，
    而密钥在那之前就去掉了 —— 一个专门用来防漂移的文件自己漂了。
    """
    body = AGENTS.read_text(encoding="utf-8")
    missing = [name for name in UNITS if name not in body]
    assert not missing, (
        f"AGENTS.md 里没提这些单元：{missing}\n"
        "（新增/改名单元时，deploy.md 有守卫拦着，AGENTS.md 也得跟上）"
    )


def test_agents_md_points_at_the_real_source_of_truth() -> None:
    """它得把读者引到 deploy.md，而不是自己再写一份会漂的副本。"""
    body = AGENTS.read_text(encoding="utf-8")
    for target in ("docs/deploy.md", "docs/handoff.md", "scripts/deploy.sh"):
        assert target in body, f"AGENTS.md 里没指向 {target}"
    assert (ROOT / "scripts" / "deploy.sh").is_file(), "AGENTS.md 说部署走 deploy.sh，但它不在"


def test_plan_service_is_not_in_the_agent_unit() -> None:
    """下载口和轮询是**两个**进程。

    合在一起的话，下载口崩一次就会把轮询带走（反之亦然），
    而这两个东西的可用性要求完全不同。
    """
    assert "serve-plan" not in _unit("yuque-agent.service")
    assert "serve-plan" in _unit("yuque-agent-plan.service")


# -- 轮询只有一个写者 + QQ 桥不在本仓库 -----------------------------------


def test_only_the_agent_unit_polls() -> None:
    """轮询/归档只能由 `yuque-agent.service` 跑。

    `state.json` 只能有一个写者——两个轮询进程互相覆盖快照是记过事故的
    （重复申请、重复通知、快照回退，见 AGENTS.md §2.2）。QQ 桥已拆成独立项目
    （它只投递通知 + 处理命令、不轮询），所以本仓库不该再出现它的单元。
    """
    agent = _unit(POLLING_UNIT)
    assert "yqa run" in agent, "轮询单元必须自己跑 `yqa run`"
    assert "--quiet-seconds" in agent, "静默期合并必须在生产里开着（它才知道这一轮改了什么）"
    assert "Conflicts=" not in agent, "核心只剩一个轮询单元，不该再声明 Conflicts"


def test_qq_unit_is_gone_from_this_repo() -> None:
    """QQ 桥（单元、命令）已经搬走——本仓库再出现这些就是回潮。"""
    units = sorted(path.name for path in (ROOT / "deploy").glob("*.service"))
    assert units == sorted(UNITS), units
    for name in UNITS:
        body = _unit(name)
        assert "qq serve" not in body, f"{name} 里不该再出现 QQ 命令"
