"""模拟社员：往知识库里写测试文档 / 清理测试文档。

⚠️ **这是开发/测试工具，不会碰「没指定的库」**：它按 ``--repo``（必填）真的往
那个知识库里写文档、删文档。**只对测试副本知识库用**——别对着生产库跑
（上线后的生产库就是 ``--repo`` 所指的那个）。

开发期用它在「当前申请目录」里造出各种形态的申请，验证整条链路：
**写文档 → 程序轮询发现变更 → 唤醒 LLM → 产出申请 JSON / 通知事件 → 留痕**。

```bash
# 写一篇（默认写进当前申请目录）
uv run python scripts/kb_sim.py --repo <group>/<test-repo> write --title "新生见面会" \
  --body-file tests/scenarios/t1_standard.md

# 写一篇草稿（正文首行保留【草稿】）
uv run python scripts/kb_sim.py --repo <group>/<test-repo> write --title "读书会" \
  --body-file tests/scenarios/t2_draft.md

# 看当前目录里有哪些文档
uv run python scripts/kb_sim.py --repo <group>/<test-repo> ls

# 清理：删掉本轮模拟写入的全部文档（按 --manifest 记录）
uv run python scripts/kb_sim.py --repo <group>/<test-repo> clean
```
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from yuque_agent import clock  # noqa: E402
from yuque_agent.config import Settings  # noqa: E402
from yuque_agent.week import cycle_of  # noqa: E402
from yuque_agent.yuque import YuqueClient, YuqueError  # noqa: E402

MANIFEST = Path("workspace/_sim_manifest.json")


def _load_manifest() -> list[dict]:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_manifest(rows: list[dict]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _client(settings: Settings) -> YuqueClient:
    return YuqueClient(host=settings.host, token=settings.token, repo=settings.repo)


def _dir_node(client: YuqueClient, title: str) -> str:
    for node in client.toc():
        if node.type == "TITLE" and node.title == title:
            return node.uuid
    raise SystemExit(f"知识库里找不到目录 {title!r}。现有目录：{[n.title for n in client.toc()]}")


def cmd_write(args: argparse.Namespace) -> None:
    settings = Settings.from_env(repo=args.repo)
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body
    if not body:
        raise SystemExit("需要 --body 或 --body-file")

    target_dir = args.dir or cycle_of(clock.today()).title
    with _client(settings) as client:
        node_uuid = _dir_node(client, target_dir)
        created = client.create_doc(title=args.title, body=body)
        doc_id = int((created or {}).get("id") or 0)
        if not doc_id:
            raise SystemExit(f"建文档失败：{created}")
        client.toc_add(doc_ids=[doc_id], target_uuid=node_uuid)
        client.wait_toc_settled()
        slug = str((created or {}).get("slug") or "")
        print(f"[write] doc_id={doc_id} title={args.title!r} -> {target_dir}")
        print(f"        {settings.host.rstrip('/')}/{settings.repo}/{slug}")

    rows = _load_manifest()
    rows.append({"doc_id": doc_id, "title": args.title, "dir": target_dir, "slug": slug})
    _save_manifest(rows)


def cmd_edit(args: argparse.Namespace) -> None:
    """改一篇已有文档的正文（模拟「社员改了申请」）。"""
    settings = Settings.from_env(repo=args.repo)
    body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file else args.body
    with _client(settings) as client:
        client.update_doc(args.doc_id, body=body)
    print(f"[edit] doc_id={args.doc_id} 已更新")


def cmd_ls(args: argparse.Namespace) -> None:
    settings = Settings.from_env(repo=args.repo)
    with _client(settings) as client:
        path_of = {n.doc_id: n.path for n in client.toc() if n.doc_id}
        rows = [d for d in client.docs() if path_of.get(d.doc_id)]
        print(f"知识库 {settings.repo}，共 {len(rows)} 篇文档：")
        for doc in sorted(rows, key=lambda d: path_of[d.doc_id]):
            print(f"  {doc.doc_id:>10}  {path_of[doc.doc_id]:<20} {doc.title}")


def cmd_clean(args: argparse.Namespace) -> None:
    settings = Settings.from_env(repo=args.repo)
    rows = _load_manifest()
    if not rows:
        print("[clean] 没有模拟记录，什么都不用做")
        return
    with _client(settings) as client:
        for row in rows:
            try:
                client.delete_doc(row["doc_id"])
                print(f"[clean] 已删除 {row['doc_id']} {row['title']!r}")
            except YuqueError as exc:
                print(f"[clean] 删除 {row['doc_id']} 失败：{exc}")
    _save_manifest([])


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟社员在知识库里写/改/清理测试文档")
    parser.add_argument(
        "--repo",
        required=True,
        help="目标知识库 namespace（必填：本项目没有默认知识库；只能指向测试副本）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="新建一篇测试文档")
    w.add_argument("--title", required=True)
    w.add_argument("--body", default="")
    w.add_argument("--body-file", default="")
    w.add_argument("--dir", default="", help="目标目录标题，默认=今天所在的那一周")
    w.set_defaults(func=cmd_write)

    e = sub.add_parser("edit", help="改一篇已有文档的正文")
    e.add_argument("--doc-id", type=int, required=True)
    e.add_argument("--body", default="")
    e.add_argument("--body-file", default="")
    e.set_defaults(func=cmd_edit)

    ls = sub.add_parser("ls", help="列出知识库里的文档")
    ls.set_defaults(func=cmd_ls)

    c = sub.add_parser("clean", help="删掉本次模拟写入的全部文档")
    c.set_defaults(func=cmd_clean)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
