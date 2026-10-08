import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentlab import filetools, pysandbox
from agentlab.tools import ToolExecutionError


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve() / "work"
        self.workspace.mkdir()
        self.context = SimpleNamespace(workspace=self.workspace, settings=None, max_output_chars=8000)

    def put(self, relative, text="", mode=None):
        target = self.workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        if mode is not None:
            target.chmod(mode)
        return target

    # ---- 受保护路径 ----
    def test_protected_paths(self):
        self.assertIn(".git", filetools.protected_reason(".git/config"))
        self.assertIn(".git", filetools.protected_reason("sub/.git/hooks/pre-commit"))
        self.assertIsNotNone(filetools.protected_reason(".env"))
        self.assertIsNotNone(filetools.protected_reason("app/.env.production"))
        self.assertIsNone(filetools.protected_reason(".env.example"))
        self.assertIsNone(filetools.protected_reason("src/environment.py"))
        self.assertIsNotNone(filetools.protected_reason("secrets/key.pem", ["*.pem"]))
        for path in (".env", ".git/config", ".git/hooks/pre-commit"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    filetools.read_file({"path": path}, self.context)
                with self.assertRaises(ValueError):
                    filetools.write_file({"path": path, "content": "x"}, self.context)
        self.assertFalse((self.workspace / ".git").exists())
        self.context.settings = {"protected_paths": ["*.pem"]}
        with self.assertRaises(ValueError):
            filetools.write_file({"path": "a/key.pem", "content": "x"}, self.context)

    # ---- 读取 ----
    def test_read_whole_file_and_size_hint(self):
        self.put("a.txt", "hello\n")
        self.assertEqual(filetools.read_file({"path": "a.txt"}, self.context), "hello\n")
        (self.workspace / "big.txt").write_bytes(b"x\n" * (filetools.MAX_FILE_BYTES // 2 + 10))
        with self.assertRaises(ValueError) as raised:
            filetools.read_file({"path": "big.txt"}, self.context)
        self.assertIn("offset/limit", str(raised.exception))
        page = filetools.read_file({"path": "big.txt", "offset": 3, "limit": 2}, self.context)
        self.assertEqual((page["start_line"], page["end_line"], page["next_offset"]), (3, 4, 5))
        self.assertGreater(page["total_lines"], 100000)

    def test_paged_read_numbers_lines_and_reports_end(self):
        self.put("n.txt", "".join("line %d\n" % i for i in range(1, 11)))
        page = filetools.read_file({"path": "n.txt", "offset": 8, "limit": 100}, self.context)
        self.assertEqual(page["content"], "     8\tline 8\n     9\tline 9\n    10\tline 10")
        self.assertEqual((page["start_line"], page["end_line"], page["total_lines"]), (8, 10, 10))
        self.assertIsNone(page["next_offset"])
        beyond = filetools.read_file({"path": "n.txt", "offset": 99}, self.context)
        self.assertEqual((beyond["content"], beyond["end_line"]), ("", 98))
        self.assertIn("总行数", beyond["note"])

    def test_paged_read_respects_output_budget_and_truncates_long_lines(self):
        self.put("wide.txt", "".join(("%d " % i) + "w" * 300 + "\n" for i in range(200)))
        self.context.max_output_chars = 4000
        page = filetools.read_file({"path": "wide.txt", "offset": 1, "limit": 5000}, self.context)
        self.assertLess(len(page["content"]), 4000)
        self.assertEqual(page["next_offset"], page["end_line"] + 1)
        self.put("long.txt", "a" * 5000 + "\n")
        shown = filetools.read_file({"path": "long.txt", "offset": 1}, self.context)["content"]
        self.assertIn("本行已截断", shown)
        (self.workspace / "bin.dat").write_bytes(b"\xff\xfe\x00bad")
        with self.assertRaises(ValueError):
            filetools.read_file({"path": "bin.dat", "offset": 1}, self.context)

    # ---- 写入与追加 ----
    def test_write_preserves_mode_of_existing_file(self):
        script = self.put("run.sh", "#!/bin/sh\n", mode=0o755)
        filetools.write_file({"path": "run.sh", "content": "#!/bin/sh\necho hi\n"}, self.context)
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)
        filetools.write_file({"path": "fresh.txt", "content": "x"}, self.context)
        self.assertEqual(stat.S_IMODE((self.workspace / "fresh.txt").stat().st_mode), 0o600)
        self.assertEqual(list(self.workspace.glob(".agentlab-*.tmp")), [])

    def test_append_creates_extends_and_enforces_limit(self):
        first = filetools.append_file({"path": "log/out.txt", "content": "one\n"}, self.context)
        self.assertEqual((first["bytes_appended"], first["total_bytes"]), (4, 4))
        second = filetools.append_file({"path": "log/out.txt", "content": "two\n"}, self.context)
        self.assertEqual(second["total_bytes"], 8)
        self.assertEqual((self.workspace / "log/out.txt").read_text(), "one\ntwo\n")
        (self.workspace / "huge.txt").write_bytes(b"x" * filetools.MAX_EDIT_BYTES)
        with self.assertRaises(ValueError):
            filetools.append_file({"path": "huge.txt", "content": "y"}, self.context)

    # ---- 编辑 ----
    def test_edit_replaces_unique_text_and_returns_diff(self):
        target = self.put("app.py", "def f():\n    return 1\n\nprint(f())\n", mode=0o755)
        result = filetools.edit_file({"path": "app.py", "old_string": "return 1", "new_string": "return 2"},
                                     self.context)
        self.assertEqual(result["replacements"], 1)
        self.assertIn("-    return 1", result["diff"])
        self.assertIn("+    return 2", result["diff"])
        self.assertEqual(target.read_text(), "def f():\n    return 2\n\nprint(f())\n")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_edit_rejects_ambiguous_missing_and_noop_edits(self):
        target = self.put("dup.txt", "a\nb\na\nc\na\n")
        before = target.read_text()
        with self.assertRaises(ValueError) as raised:
            filetools.edit_file({"path": "dup.txt", "old_string": "a", "new_string": "z"}, self.context)
        self.assertIn("3 次", str(raised.exception))
        self.assertIn("1, 3, 5", str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            filetools.edit_file({"path": "dup.txt", "old_string": "nope", "new_string": "z"}, self.context)
        self.assertIn("没有找到", str(raised.exception))
        for arguments in ({"old_string": "a", "new_string": "a"}, {"old_string": "", "new_string": "x"}):
            with self.assertRaises(ValueError):
                filetools.edit_file({"path": "dup.txt", **arguments}, self.context)
        with self.assertRaises(ValueError) as raised:
            filetools.edit_file({"path": "absent.txt", "old_string": "a", "new_string": "b"}, self.context)
        self.assertIn("write_file", str(raised.exception))
        self.assertEqual(target.read_text(), before)
        done = filetools.edit_file({"path": "dup.txt", "old_string": "a", "new_string": "z", "replace_all": True},
                                   self.context)
        self.assertEqual(done["replacements"], 3)
        self.assertEqual(target.read_text(), "z\nb\nz\nc\nz\n")

    def test_edit_refuses_symlink_and_protected_targets(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("secret")
        (self.workspace / "link.txt").symlink_to(outside)
        with self.assertRaises((ValueError, OSError)):
            filetools.edit_file({"path": "link.txt", "old_string": "secret", "new_string": "x"}, self.context)
        self.assertEqual(outside.read_text(), "secret")
        self.put(".env", "KEY=1\n")
        with self.assertRaises(ValueError):
            filetools.edit_file({"path": ".env", "old_string": "1", "new_string": "2"}, self.context)

    # ---- glob ----
    def test_glob_patterns(self):
        for path in ("a.py", "src/b.py", "src/deep/c.py", "src/test_x.py", "tests/test_y.py", "web/app.js",
                     "web/app.ts", "README.md", "node_modules/pkg/index.py", ".git/HEAD", "__pycache__/z.py",
                     ".env", ".env.example"):
            self.put(path, "x")

        def find(pattern, **extra):
            return filetools.glob_files({"pattern": pattern, **extra}, self.context)["matches"]

        self.assertEqual(find("*.py"), ["a.py", "src/b.py", "src/deep/c.py", "src/test_x.py", "tests/test_y.py"])
        self.assertEqual(find("**/test_*.py"), ["src/test_x.py", "tests/test_y.py"])
        self.assertEqual(find("src/*.py"), ["src/b.py", "src/test_x.py"])
        self.assertEqual(find("src/**/*.py"), ["src/b.py", "src/deep/c.py", "src/test_x.py"])
        self.assertEqual(find("web/*.{js,ts}"), ["web/app.js", "web/app.ts"])
        self.assertEqual(find("app.?s"), ["web/app.js", "web/app.ts"])
        self.assertEqual(find("*.py", path="src"), ["src/b.py", "src/deep/c.py", "src/test_x.py"])
        self.assertEqual(find("./README.md"), ["README.md"])
        # 忽略目录、受保护文件不会出现；模板文件可以。
        everything = find("**/*")
        for hidden in ("node_modules/pkg/index.py", ".git/HEAD", "__pycache__/z.py", ".env"):
            self.assertNotIn(hidden, everything)
        self.assertIn(".env.example", everything)
        with self.assertRaises(ValueError):
            find("*", path="../outside")
        with self.assertRaises(ValueError):
            find("*", path="missing")

    def test_glob_limit_and_symlinks(self):
        for index in range(10):
            self.put("many/f%02d.txt" % index, "x")
        result = filetools.glob_files({"pattern": "*.txt", "limit": 4}, self.context)
        self.assertEqual((result["count"], result["truncated"]), (4, True))
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("x")
        (self.workspace / "linkdir").symlink_to(outside)
        (self.workspace / "linkfile.txt").symlink_to(outside / "leak.txt")
        found = filetools.glob_files({"pattern": "*.txt", "limit": 100}, self.context)["matches"]
        self.assertNotIn("linkdir/leak.txt", found)
        self.assertNotIn("linkfile.txt", found)

    def test_glob_regex_rejects_nothing_dangerous(self):
        for pattern in ("[", "a{b", "*[!a]*", "{a,b}{c,d}"):
            filetools.compile_glob(pattern)
        with self.assertRaises(ValueError):
            filetools.compile_glob("")
        with self.assertRaises(ValueError):
            filetools.compile_glob("x" * 501)

    # ---- grep ----
    def payload(self, **extra):
        return {"workspace": str(self.workspace), "pattern": "x", **extra}

    def test_grep_tree_modes(self):
        self.put("a.py", "import os\nTODO fix\ndef run():\n    pass\n# todo later\n")
        self.put("b.txt", "nothing here\nTODO(b)\n")
        self.put("sub/c.py", "def run2():\n    return 'a.b'\n")
        (self.workspace / "blob.bin").write_bytes(b"TODO\x00binary")
        self.put(".env", "TODO=secret\n")
        self.put("node_modules/m.js", "TODO\n")

        found = filetools.grep_tree(self.payload(pattern="TODO"))
        self.assertEqual([(m["path"], m["line"]) for m in found["matches"]], [("a.py", 2), ("b.txt", 2)])
        # 只搜索 a.py、b.txt、sub/c.py；.env、node_modules 与二进制文件不计入。
        self.assertEqual(found["files_searched"], 3)
        both = filetools.grep_tree(self.payload(pattern="todo", ignore_case=True))
        self.assertEqual(len(both["matches"]), 3)
        scoped = filetools.grep_tree(self.payload(pattern="def \\w+", glob="*.py"))
        self.assertEqual([m["text"] for m in scoped["matches"]], ["def run():", "def run2():"])
        in_dir = filetools.grep_tree(self.payload(pattern="def", path="sub"))
        self.assertEqual([m["path"] for m in in_dir["matches"]], ["sub/c.py"])
        literal = filetools.grep_tree(self.payload(pattern="'a.b'", fixed=True))
        self.assertEqual(len(literal["matches"]), 1)
        self.assertEqual(len(filetools.grep_tree(self.payload(pattern="a.b"))["matches"]), 1)
        around = filetools.grep_tree(self.payload(pattern="def run\\(", context=1))["matches"][0]
        self.assertEqual((around["before"], around["after"]), (["TODO fix"], ["    pass"]))
        capped = filetools.grep_tree(self.payload(pattern=".", limit=2))
        self.assertEqual((len(capped["matches"]), capped["truncated"]), (2, True))
        with self.assertRaises(ValueError) as raised:
            filetools.grep_tree(self.payload(pattern="(unclosed"))
        self.assertIn("fixed=true", str(raised.exception))

    def test_grep_files_runs_in_subprocess(self):
        self.put("m.py", "alpha\nbeta\n")
        result = asyncio.run(filetools.grep_files({"pattern": "beta"}, self.context))
        self.assertEqual([(m["path"], m["line"]) for m in result["matches"]], [("m.py", 2)])
        with self.assertRaises(ValueError) as raised:
            asyncio.run(filetools.grep_files({"pattern": "[bad"}, self.context))
        self.assertIn("正则", str(raised.exception))

    def test_grep_kills_catastrophic_regex(self):
        self.put("trap.txt", "a" * 60 + "b\n")
        with patch.object(filetools, "GREP_TIMEOUT", 1.0):
            with self.assertRaises(ValueError) as raised:
                asyncio.run(filetools.grep_files({"pattern": "(a+)+$"}, self.context))
        self.assertIn("已终止", str(raised.exception))
        # 子进程被杀掉后，同样的文件用字面搜索仍然立即可用。
        result = asyncio.run(filetools.grep_files({"pattern": "b", "fixed": True}, self.context))
        self.assertEqual(result["count"], 1)


class RunCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve()

    async def test_exit_codes_and_streams(self):
        ok = await pysandbox.run_command("echo out; echo err >&2", self.cwd, shell=True)
        self.assertEqual((ok["ok"], ok["exit_code"], ok["stdout"], ok["stderr"]), (True, 0, "out\n", "err\n"))
        failed = await pysandbox.run_command("echo partial; exit 3", self.cwd, shell=True)
        self.assertEqual((failed["ok"], failed["exit_code"], failed["stdout"]), (False, 3, "partial\n"))
        self.assertIn("退出码 3", failed["error"])
        self.assertEqual((await pysandbox.run_command(["pwd"], self.cwd))["stdout"].strip(), str(self.cwd))

    async def test_missing_executable_and_stdin(self):
        missing = await pysandbox.run_command(["definitely-not-a-binary-xyz"], self.cwd)
        self.assertEqual((missing["ok"], missing["exit_code"]), (False, 127))
        self.assertIn("找不到可执行文件", missing["error"])
        echoed = await pysandbox.run_command(["cat"], self.cwd, stdin_text="来自 stdin")
        self.assertEqual(echoed["stdout"], "来自 stdin")

    async def test_timeout_kills_whole_process_group(self):
        marker = self.cwd / "survivor.txt"
        command = "(sleep 2; echo alive > %s) & sleep 30" % marker
        outcome = await pysandbox.run_command(command, self.cwd, shell=True, timeout=0.5)
        self.assertTrue(outcome["timed_out"])
        self.assertFalse(outcome["ok"])
        await asyncio.sleep(2.5)
        self.assertFalse(marker.exists(), "后台子进程在超时后仍然存活")

    async def test_timeout_does_not_let_the_shell_run_the_next_command(self):
        """`pytest; rm -rf build`：超时杀掉 pytest 的瞬间，shell 不能再把后面的命令执行掉。

        直接对进程组发 SIGKILL 是逐个投递的，先死的子进程会让 shell 在自己被杀之前继续往下跑；
        先 SIGSTOP 冻结整组可以避免。并发跑多个场景以放大这个竞争窗口。
        """
        async def scenario(index):
            marker = self.cwd / ("ran-%d" % index)
            outcome = await pysandbox.run_command("sleep 30; echo ran > %s" % marker, self.cwd, shell=True,
                                                  timeout=0.3)
            return marker, outcome["timed_out"]

        results = await asyncio.gather(*[scenario(i) for i in range(24)])
        await asyncio.sleep(0.6)
        self.assertTrue(all(timed_out for _, timed_out in results))
        leaked = [marker.name for marker, _ in results if marker.exists()]
        self.assertEqual(leaked, [], "超时后 shell 仍然执行了后续命令")

    async def test_output_keeps_head_and_tail(self):
        outcome = await pysandbox.run_command(
            "i=0; while [ $i -lt 4000 ]; do echo line-$i; i=$((i+1)); done; echo FINAL-SUMMARY",
            self.cwd, shell=True, max_output_chars=1000)
        self.assertTrue(outcome["truncated"])
        self.assertIn("line-0", outcome["stdout"])
        self.assertIn("FINAL-SUMMARY", outcome["stdout"])
        self.assertIn("中间省略", outcome["stdout"])
        self.assertLess(len(outcome["stdout"]), 1100)

    async def test_environment_is_sanitized_and_non_interactive(self):
        with patch.dict(os.environ, {"AGENTLAB_API_KEY": "sk-secret", "OPENAI_API_KEY": "sk-other",
                                     "HTTPS_PROXY": "http://proxy"}):
            outcome = await pysandbox.run_command("env", self.cwd, shell=True)
        self.assertNotIn("sk-secret", outcome["stdout"])
        self.assertNotIn("sk-other", outcome["stdout"])
        self.assertNotIn("proxy", outcome["stdout"])
        self.assertIn("GIT_TERMINAL_PROMPT=0", outcome["stdout"])
        self.assertIn("PAGER=cat", outcome["stdout"])

    async def test_validation(self):
        for kwargs in ({"timeout": 0}, {"timeout": 10_000}, {"memory_mb": 1}, {"max_output_chars": 10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await pysandbox.run_command("true", self.cwd, shell=True, **kwargs)
        for command in ("", [], ["ls", 3]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                await pysandbox.run_command(command, self.cwd, shell=isinstance(command, str))

    def test_clip_ends(self):
        self.assertEqual(pysandbox.clip_ends("short", 100), "short")
        clipped = pysandbox.clip_ends("H" * 100 + "M" * 1000 + "T" * 100, 300)
        self.assertTrue(clipped.startswith("H" * 100))
        self.assertTrue(clipped.endswith("T" * 100))
        self.assertIn("中间省略", clipped)


if __name__ == "__main__":
    unittest.main()
