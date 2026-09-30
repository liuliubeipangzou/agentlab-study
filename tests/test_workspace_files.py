"""工作区文件交换的测试：路径安全、上传回滚、列表与下载边界。

这个模块此前完全没有测试覆盖，而它直接暴露在本机 HTTP 接口上。
"""

import base64
import os
import pathlib
import tempfile
import unittest

from agentlab.workspace_files import (MAX_DOWNLOAD_BYTES, MAX_UPLOAD_BYTES,
                                      import_files, list_workspace, read_file)


def encoded(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


class WorkspaceTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.workspace = self._temp.name

    def names(self):
        return sorted(item.name for item in pathlib.Path(self.workspace).iterdir())

    def write(self, name, text="content"):
        path = pathlib.Path(self.workspace) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class PathSafetyTests(WorkspaceTestCase):
    def test_rejects_traversal_hidden_and_malformed_paths(self):
        for path in ("../outside.txt", "sub/../../x", ".hidden", "a/.b",
                     "/absolute", "back\\slash", "a//b", "trailing/", "",
                     "a" * 1025, "control\x01char"):
            with self.subTest(path=path[:24]):
                with self.assertRaises(ValueError):
                    read_file(self.workspace, path)

    def test_rejects_symlink_target(self):
        self.write("real.txt", "secret")
        os.symlink(str(pathlib.Path(self.workspace) / "real.txt"),
                   str(pathlib.Path(self.workspace) / "link.txt"))
        with self.assertRaises(ValueError):
            read_file(self.workspace, "link.txt")

    def test_rejects_file_behind_symlinked_directory(self):
        self.write("outside/secret.txt", "secret")
        os.symlink(str(pathlib.Path(self.workspace) / "outside"),
                   str(pathlib.Path(self.workspace) / "linked"))
        with self.assertRaises(ValueError):
            read_file(self.workspace, "linked/secret.txt")

    def test_rejects_non_regular_files(self):
        fifo = pathlib.Path(self.workspace) / "pipe"
        try:
            os.mkfifo(str(fifo))
        except (AttributeError, OSError):
            self.skipTest("当前平台不支持 mkfifo")
        with self.assertRaises(ValueError):
            read_file(self.workspace, "pipe")

    def test_reads_normal_nested_file(self):
        self.write("sub/note.txt", "内容")
        result = read_file(self.workspace, "sub/note.txt")
        self.assertEqual(base64.b64decode(result["content_base64"]).decode("utf-8"), "内容")
        self.assertEqual(result["size"], 6)


class ListingTests(WorkspaceTestCase):
    def test_lists_files_and_directories_without_hidden_entries(self):
        self.write("a.txt", "aaa")
        self.write("sub/b.txt", "bb")
        self.write(".hidden.txt", "hidden")
        result = list_workspace(self.workspace)
        paths = {row["path"]: row["kind"] for row in result["files"]}
        self.assertEqual(paths.get("a.txt"), "file")
        self.assertEqual(paths.get("sub"), "directory")
        self.assertEqual(paths.get("sub/b.txt"), "file")
        self.assertNotIn(".hidden.txt", paths)

    def test_sizes_are_reported_for_files_only(self):
        self.write("a.txt", "12345")
        rows = {row["path"]: row for row in list_workspace(self.workspace)["files"]}
        self.assertEqual(rows["a.txt"]["size"], 5)

    def test_parameter_validation(self):
        for kwargs in ({"max_entries": 0}, {"max_entries": 501}, {"max_entries": "10"},
                       {"max_depth": 0}, {"max_depth": 7}, {"max_depth": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    list_workspace(self.workspace, **kwargs)

    def test_respects_entry_limit_and_reports_truncation(self):
        for index in range(20):
            self.write("f%02d.txt" % index, "x")
        result = list_workspace(self.workspace, max_entries=5)
        self.assertLessEqual(len(result["files"]), 5)
        self.assertTrue(result["truncated"])

    def test_depth_limit_marks_truncated(self):
        self.write("l1/l2/l3/deep.txt", "x")
        result = list_workspace(self.workspace, max_depth=1)
        self.assertTrue(result["truncated"])

    def test_limits_are_advertised(self):
        result = list_workspace(self.workspace)
        self.assertEqual(result["max_file_bytes"], MAX_UPLOAD_BYTES)
        self.assertEqual(result["max_download_bytes"], MAX_DOWNLOAD_BYTES)


class ImportTests(WorkspaceTestCase):
    def test_uploads_new_files(self):
        result = import_files(self.workspace, {"files": [
            {"name": "a.txt", "content_base64": encoded("AAA")},
            {"name": "b.txt", "content_base64": encoded("BBBB")}]})
        self.assertEqual([row["path"] for row in result["files"]], ["a.txt", "b.txt"])
        self.assertEqual(self.names(), ["a.txt", "b.txt"])
        self.assertEqual(pathlib.Path(self.workspace, "a.txt").read_text(), "AAA")

    def test_never_overwrites_existing_file(self):
        self.write("a.txt", "original")
        with self.assertRaises(ValueError) as caught:
            import_files(self.workspace, {"files": [
                {"name": "a.txt", "content_base64": encoded("replacement")}]})
        self.assertIn("已存在", str(caught.exception))
        self.assertEqual(pathlib.Path(self.workspace, "a.txt").read_text(), "original")

    def test_partial_failure_rolls_back_created_files(self):
        """第二个文件重名时，第一个不能留在工作区。"""
        self.write("taken.txt", "original")
        with self.assertRaises(ValueError):
            import_files(self.workspace, {"files": [
                {"name": "fresh.txt", "content_base64": encoded("new")},
                {"name": "taken.txt", "content_base64": encoded("dup")}]})
        self.assertEqual(self.names(), ["taken.txt"])
        self.assertFalse(pathlib.Path(self.workspace, "fresh.txt").exists())

    def test_no_temporary_files_leak_after_failure(self):
        self.write("taken.txt", "original")
        with self.assertRaises(ValueError):
            import_files(self.workspace, {"files": [
                {"name": "fresh.txt", "content_base64": encoded("new")},
                {"name": "taken.txt", "content_base64": encoded("dup")}]})
        self.assertFalse([name for name in self.names() if name.startswith(".upload-")])

    def test_payload_validation(self):
        cases = [
            {}, {"files": []}, {"files": "x"},
            {"files": [{"name": "a.txt", "content_base64": encoded("x")}] * 11},
            {"files": ["not-a-dict"]},
            {"files": [{"name": "sub/a.txt", "content_base64": encoded("x")}]},
            {"files": [{"name": ".hidden", "content_base64": encoded("x")}]},
            {"files": [{"name": "../x", "content_base64": encoded("x")}]},
            {"files": [{"name": "a.txt", "content_base64": "not base64!!"}]},
            {"files": [{"name": "a.txt", "content_base64": 123}]},
            {"files": [{"name": "a.txt", "content_base64": encoded("x")},
                       {"name": "a.txt", "content_base64": encoded("y")}]},
            {"files": [{"name": "n" * 241 + ".txt", "content_base64": encoded("x")}]},
        ]
        for payload in cases:
            with self.subTest(payload=str(payload)[:48]):
                with self.assertRaises(ValueError):
                    import_files(self.workspace, payload)

    def test_size_boundary(self):
        exact = import_files(self.workspace, {"files": [
            {"name": "exact.bin", "content_base64": encoded(b"x" * MAX_UPLOAD_BYTES)}]})
        self.assertEqual(exact["files"][0]["size"], MAX_UPLOAD_BYTES)

    def test_oversized_file_is_rejected_without_trace(self):
        payload = {"files": [{"name": "big.bin",
                              "content_base64": encoded(b"x" * (MAX_UPLOAD_BYTES + 1))}]}
        with self.assertRaises(ValueError) as caught:
            import_files(self.workspace, payload)
        self.assertIn("1 MiB", str(caught.exception))
        self.assertEqual(self.names(), [])

    def test_total_size_limit_across_files(self):
        half = b"x" * (MAX_UPLOAD_BYTES // 2 + 1)
        with self.assertRaises(ValueError) as caught:
            import_files(self.workspace, {"files": [
                {"name": "a.bin", "content_base64": encoded(half)},
                {"name": "b.bin", "content_base64": encoded(half)}]})
        self.assertIn("总大小", str(caught.exception))
        self.assertEqual(self.names(), [])


class DownloadTests(WorkspaceTestCase):
    def test_rejects_oversized_download(self):
        with open(os.path.join(self.workspace, "big.bin"), "wb") as stream:
            stream.truncate(MAX_DOWNLOAD_BYTES + 1)
        with self.assertRaises(ValueError) as caught:
            read_file(self.workspace, "big.bin")
        self.assertIn("5 MiB", str(caught.exception))

    def test_binary_content_round_trips(self):
        blob = bytes(range(256))
        import_files(self.workspace, {"files": [
            {"name": "blob.bin", "content_base64": encoded(blob)}]})
        result = read_file(self.workspace, "blob.bin")
        self.assertEqual(base64.b64decode(result["content_base64"]), blob)

    def test_missing_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            read_file(self.workspace, "nope.txt")

    def test_missing_workspace_raises_value_error(self):
        with self.assertRaises(ValueError):
            list_workspace(os.path.join(self.workspace, "does-not-exist"))


if __name__ == "__main__":
    unittest.main()
