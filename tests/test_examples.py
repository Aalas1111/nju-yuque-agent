"""守卫：`examples/` 里的契约示例必须和**真代码产出的键**一致。

为什么要有这条：`examples/*.json` 是给下游（谷和平、王恩成、cac）照抄的，
而「照抄一份过期的东西」是这轮改动里已经踩到两次的坑（systemd 单元、
deploy.md 的目录图）。示例比文档更危险——它们会被直接当接口定义用。

所以这里不查「字段值对不对」（那是契约测试的事），只查**键集合**：
示例里多一个键、少一个键，都说明契约变了而示例没跟上。

同理，`plan_updated` 这种新增的 kind 也必须在示例/契约里体现出来。
"""

from __future__ import annotations

import json
from pathlib import Path

from yuque_agent import outputs
from yuque_agent.config import Settings
from yuque_agent.outputs import CRB_ACTIVITY_FIELDS

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def _keys(payload: dict, *, ignore_comment: bool = True) -> set[str]:
    keys = set(payload)
    if ignore_comment:
        keys.discard("_comment")
    return keys


def _settings(tmp_path: Path) -> Settings:
    s = Settings(repo="g/kb", workspace=tmp_path / "ws")
    s.ensure_dirs()
    return s


# -- application.example.json ---------------------------------------------


def test_application_example_matches_the_real_record(tmp_path: Path) -> None:
    from .test_outputs import application_payload

    example = _example("application.example.json")
    settings = _settings(tmp_path)
    result = outputs.write_application(settings, application_payload())
    record = json.loads(Path(result["path"]).read_text(encoding="utf-8"))

    assert _keys(example) == _keys(record), (
        "examples/application.example.json 的键和 write_application 产出的不一致。\n"
        f"  示例多出来的: {sorted(_keys(example) - _keys(record))}\n"
        f"  示例少了的  : {sorted(_keys(record) - _keys(example))}"
    )
    # activity 里只该有 crb 认得的字段
    assert set(example["activity"]) == set(CRB_ACTIVITY_FIELDS)


def test_application_example_carries_the_cycle(tmp_path: Path) -> None:
    """`cycle` 是这轮新增的，示例必须带上（下游要靠它判断这批属于哪一周）。"""
    assert _example("application.example.json")["cycle"]


# -- plan.example.json ----------------------------------------------------


def test_plan_example_matches_the_real_plan(tmp_path: Path) -> None:
    example = _example("plan.example.json")
    settings = _settings(tmp_path)
    plan = outputs.build_plan_json(settings)
    assert _keys(example) == _keys(plan), (
        "examples/plan.example.json 的键和 build_plan_json 产出的不一致。\n"
        f"  示例多出来的: {sorted(_keys(example) - _keys(plan))}\n"
        f"  示例少了的  : {sorted(_keys(plan) - _keys(example))}"
    )


def test_plan_example_activities_only_use_contract_fields(tmp_path: Path) -> None:
    example = _example("plan.example.json")
    allowed = set(CRB_ACTIVITY_FIELDS) | {"_application_id", "_doc_id"}
    for entry in example["activities"]:
        extra = set(entry) - allowed
        assert not extra, f"plan 的 activity 里混进了 crb 不认的键：{extra}"


# -- notice.example.json --------------------------------------------------


def test_notice_example_matches_the_real_record(tmp_path: Path) -> None:
    example = _example("notice.example.json")
    settings = _settings(tmp_path)
    result = outputs.write_notice(
        settings,
        kind="rejected",
        payload={"summary": "x", "message": "y", "member": {"name": "孙七"}},
    )
    record = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert _keys(example) == _keys(record), (
        "examples/notice.example.json 的键和 write_notice 产出的不一致。\n"
        f"  示例多出来的: {sorted(_keys(example) - _keys(record))}\n"
        f"  示例少了的  : {sorted(_keys(record) - _keys(example))}"
    )


def test_every_notice_kind_is_documented_in_the_example_or_handoff() -> None:
    """每个 kind 至少要在**示例的注释或 handoff 的表格**里能被查到。

    否则下游拿到一条 `kind: "plan_updated"` 不知道那是什么。
    （已经有过教训：`accepted` 在契约里、在提示词里都是零命中。）
    """
    handoff = (EXAMPLES.parent / "docs" / "handoff.md").read_text(encoding="utf-8")
    example = (EXAMPLES / "notice.example.json").read_text(encoding="utf-8")
    haystack = handoff + example
    missing = [kind for kind in outputs.NOTICE_KINDS if kind not in haystack]
    assert not missing, f"这些 kind 在下游文档里查不到：{missing}"
