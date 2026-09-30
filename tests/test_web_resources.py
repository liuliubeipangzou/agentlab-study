"""校验文档快照同步及 package-data 覆盖；不安装软件、不访问网络。"""
import ast
import fnmatch
from pathlib import Path
import re
import tempfile
import unittest

from scripts.sync_web_resources import sync_resources


ROOT = Path(__file__).resolve().parents[1]


class WebResourceTests(unittest.TestCase):
    def project(self, root):
        (root / "docs").mkdir()
        (root / "knowledge").mkdir()
        (root / "README.md").write_text("# 使用说明\n", encoding="utf-8")
        (root / "docs" / "guide.md").write_text("# 学习指南\n", encoding="utf-8")
        (root / "knowledge" / "example.md").write_text("# 示例知识\n", encoding="utf-8")

    def test_sync_creates_updates_and_removes_only_generated_markdown(self):
        with tempfile.TemporaryDirectory(prefix="agentlab-resource-test-") as temporary:
            root = Path(temporary)
            self.project(root)
            first = sync_resources(root)
            self.assertEqual(first, {"docs": 2, "knowledge": 1, "updated": 3, "removed": 0})
            output = root / "agentlab" / "resources"
            self.assertEqual((output / "docs" / "README.md").read_bytes(), (root / "README.md").read_bytes())
            self.assertEqual(sync_resources(root)["updated"], 0)
            (root / "docs" / "guide.md").unlink()
            (root / "docs" / "advanced.md").write_text("新版指南", encoding="utf-8")
            (root / "knowledge" / "example.md").write_text("新版知识", encoding="utf-8")
            (output / "docs" / "keep.txt").write_text("unrelated")
            updated = sync_resources(root)
            self.assertEqual((updated["updated"], updated["removed"]), (2, 1))
            self.assertFalse((output / "docs" / "guide.md").exists())
            self.assertTrue((output / "docs" / "advanced.md").is_file())
            self.assertEqual((output / "knowledge" / "example.md").read_text(encoding="utf-8"), "新版知识")
            self.assertEqual((output / "docs" / "keep.txt").read_text(), "unrelated")

    def test_check_reports_drift_without_writing(self):
        with tempfile.TemporaryDirectory(prefix="agentlab-resource-test-") as temporary:
            root = Path(temporary)
            self.project(root)
            checked = sync_resources(root, check=True)
            self.assertEqual(checked["updated"], 3)
            self.assertFalse((root / "agentlab").exists())
            sync_resources(root)
            (root / "README.md").write_text("new readme")
            snapshot = root / "agentlab" / "resources" / "docs" / "README.md"
            before = snapshot.read_bytes()
            self.assertEqual(sync_resources(root, check=True)["updated"], 1)
            self.assertEqual(snapshot.read_bytes(), before)

    def test_invalid_source_does_not_create_partial_snapshots(self):
        with tempfile.TemporaryDirectory(prefix="agentlab-resource-test-") as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                sync_resources(root)
            self.assertFalse((root / "agentlab").exists())

    def test_package_globs_cover_web_docs_and_all_three_knowledge_files(self):
        # 本项目 package-data 是普通字符串数组，可用标准库读取而不新增 TOML 依赖。
        configuration = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        section = configuration.split("[tool.setuptools.package-data]", 1)[1]
        match = re.search(r"agentlab\s*=\s*(\[[^\]]+\])", section, re.DOTALL)
        self.assertIsNotNone(match)
        patterns = ast.literal_eval(match.group(1))
        expected = ["web/index.html", "web/style.css", "web/app.js"]
        expected += ["resources/docs/README.md"]
        expected += ["resources/docs/" + path.name for path in (ROOT / "docs").glob("*.md")]
        examples = list((ROOT / "knowledge").glob("*.md"))
        self.assertEqual(len(examples), 3)
        expected += ["resources/knowledge/" + path.name for path in examples]
        for relative in expected:
            with self.subTest(resource=relative):
                self.assertTrue(any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns), relative)
                self.assertTrue((ROOT / "agentlab" / relative).is_file(), relative)


if __name__ == "__main__":
    unittest.main()
