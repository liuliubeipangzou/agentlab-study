"""将学习文档和示例知识同步为安装包资源；开发者修改原文后运行此脚本。"""
import argparse
from pathlib import Path


def sync_resources(project_root=None, check=False):
    """先读取完整来源，再更新快照；check 模式只报告差异，不修改任何文件。"""
    root = Path(project_root) if project_root is not None else Path(__file__).resolve().parents[1]
    root = root.resolve()
    documentation = root / "docs"
    knowledge = root / "knowledge"
    readme = root / "README.md"
    if not documentation.is_dir() or not knowledge.is_dir() or not readme.is_file():
        raise ValueError("项目必须包含 README.md、docs/ 和 knowledge/。")
    snapshots = {
        "docs": {path.name: path.read_bytes() for path in sorted(documentation.glob("*.md")) if path.is_file()},
        "knowledge": {path.name: path.read_bytes() for path in sorted(knowledge.glob("*.md")) if path.is_file()},
    }
    snapshots["docs"]["README.md"] = readme.read_bytes()
    if not snapshots["knowledge"]:
        raise ValueError("knowledge/ 中没有可同步的 Markdown 示例。")
    result = {"docs": len(snapshots["docs"]), "knowledge": len(snapshots["knowledge"]),
              "updated": 0, "removed": 0}
    for section, files in snapshots.items():
        destination = root / "agentlab" / "resources" / section
        if not check:
            destination.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            target = destination / name
            if not target.is_file() or target.read_bytes() != content:
                result["updated"] += 1
                if not check:
                    target.write_bytes(content)
        # resources 下的 Markdown 仅为生成快照，来源删除或改名后移除旧快照。
        for stale in sorted(destination.glob("*.md")):
            if stale.name not in files:
                result["removed"] += 1
                if not check:
                    stale.unlink()
    return result


def main(argv=None):
    """直接运行同步；--check 用于检查快照是否需要更新。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只检查差异，不更新快照")
    args = parser.parse_args(argv)
    result = sync_resources(check=args.check)
    print("学习文档 {docs} 份，示例知识 {knowledge} 份；{updated} 份需更新，{removed} 份过期。".format(**result)
          if args.check else "已同步学习文档 {docs} 份、示例知识 {knowledge} 份；更新 {updated} 份，移除 {removed} 份。".format(**result))
    return 1 if args.check and (result["updated"] or result["removed"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
