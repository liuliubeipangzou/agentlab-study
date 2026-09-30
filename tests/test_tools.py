"""验证工具执行的真实边界：校验、授权、路径、资源与取消。"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agentlab.tools import MAX_FILE_BYTES, Tool, ToolContext, ToolRegistry, create_builtin_tools, validate_schema
from agentlab.types import ToolCall


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.context = ToolContext(self.workspace)
        self.registry = create_builtin_tools()

    def tearDown(self):
        self.directory.cleanup()

    async def call(self, name, arguments, approved=False):
        return await self.registry.execute(ToolCall(name, arguments), self.context, approved)

    async def test_calculator_and_no_code_execution(self):
        self.assertEqual((await self.call("calculator", {"expression": "(7 + 5) * 3 / 2"}))["value"], 18)
        self.assertEqual((await self.call("calculator", {"expression": "2 ** -3"}))["value"], 0.125)
        forbidden = ["__import__('os').getcwd()", "(1).__class__", "[1,2]", "True + 1",
                     "2 ** 100000000", "10 ** 101", "1e999", "1/0", "(-1) ** .5",
                     "1+" * 100 + "1"]
        for expression in forbidden:
            with self.subTest(expression=expression):
                result = await self.call("calculator", {"expression": expression})
                self.assertFalse(result["ok"], result)

    async def test_write_requires_approval_and_roundtrips_unicode(self):
        arguments = {"path": "notes/学习.txt", "content": "Agent 工具调用\n"}
        call = ToolCall("write_file", arguments)
        self.assertTrue(self.registry.requires_approval(call))
        self.assertFalse((await self.registry.execute(call, self.context))["ok"])
        self.assertFalse((self.workspace / "notes").exists())
        self.assertFalse((await self.registry.execute(call, self.context, approved="yes"))["ok"])
        self.assertTrue((await self.registry.execute(call, self.context, approved=True))["ok"])
        result = await self.call("read_file", {"path": arguments["path"]})
        self.assertEqual(result["value"], arguments["content"])
        self.assertTrue((await self.call("write_file", {**arguments, "content": "replaced"}, True))["ok"])
        self.assertEqual((self.workspace / arguments["path"]).read_text(), "replaced")
        self.assertEqual(list((self.workspace / "notes").glob(".agentlab-*.tmp")), [])

    async def test_path_traversal_absolute_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / "secret.txt"
            secret.write_text("outside")
            (self.workspace / "link.txt").symlink_to(secret)
            (self.workspace / "linked-directory").symlink_to(Path(outside), target_is_directory=True)
            for path in ("../secret.txt", str(secret), "link.txt", "linked-directory/secret.txt",
                         "notes/../../secret.txt", "./secret.txt", "notes\\secret.txt"):
                with self.subTest(path=path):
                    self.assertFalse((await self.call("read_file", {"path": path}))["ok"])
                    self.assertFalse((await self.call("write_file", {"path": path, "content": "changed"}, True))["ok"])
            self.assertEqual(secret.read_text(), "outside")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX named pipe test")
    async def test_non_regular_file_is_rejected_without_blocking(self):
        os.mkfifo(self.workspace / "fifo")
        result = await asyncio.wait_for(self.call("read_file", {"path": "fifo"}), timeout=1)
        self.assertFalse(result["ok"])
        self.assertIn("regular", result["error"])
        self.assertFalse((await self.call("write_file", {"path": "fifo", "content": "x"}, True))["ok"])

    async def test_file_size_and_utf8_limits(self):
        (self.workspace / "large.txt").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
        self.assertFalse((await self.call("read_file", {"path": "large.txt"}))["ok"])
        result = await self.call("write_file", {"path": "large-write.txt", "content": "学" * MAX_FILE_BYTES}, True)
        self.assertFalse(result["ok"])
        self.assertFalse((self.workspace / "large-write.txt").exists())
        (self.workspace / "binary.bin").write_bytes(b"\xff\xfe")
        self.assertFalse((await self.call("read_file", {"path": "binary.bin"}))["ok"])

    async def test_failed_atomic_replace_preserves_original_and_cleans_temp(self):
        target = self.workspace / "important.txt"
        target.write_text("original")
        with mock.patch("agentlab.tools.os.replace", side_effect=OSError("disk failure")):
            result = await self.call("write_file", {"path": "important.txt", "content": "changed"}, True)
        self.assertFalse(result["ok"])
        self.assertEqual(target.read_text(), "original")
        self.assertEqual(list(self.workspace.glob(".agentlab-*.tmp")), [])

    async def test_validation_precedes_handler(self):
        hits = []

        async def capture(arguments, context):
            hits.append(arguments)
            return arguments

        schema = {"type": "object", "properties": {
            "record": {"type": "object", "properties": {
                "mode": {"type": "string", "enum": ["learn", "run"]},
                "scores": {"type": "array", "items": {"type": "number", "minimum": 0},
                           "minItems": 1, "maxItems": 3},
                "enabled": {"type": "boolean"}},
                "required": ["mode", "scores", "enabled"], "additionalProperties": False}},
            "required": ["record"], "additionalProperties": False}
        self.registry.register(Tool("validate", "schema exercise", schema, capture))
        good = {"record": {"mode": "learn", "scores": [0, 1.5], "enabled": True}}
        self.assertTrue((await self.call("validate", good))["ok"])
        invalid = [{}, {**good, "extra": 1}, {"record": []},
                   {"record": {**good["record"], "mode": "bad"}},
                   {"record": {**good["record"], "scores": [True]}},
                   {"record": {**good["record"], "scores": [float("nan")]}},
                   {"record": {**good["record"], "scores": [float("inf")]}},
                   {"record": {**good["record"], "scores": [-1]}},
                   {"record": {**good["record"], "enabled": 1}},
                   {"record": {**good["record"], "surprise": 1}},
                   {"record": {**good["record"], "scores": []}},
                   {"record": {**good["record"], "scores": [1, 2, 3, 4]}}]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assertFalse((await self.call("validate", arguments))["ok"])
        self.assertEqual(len(hits), 1)

    async def test_additional_property_schema_enum_and_deep_json(self):
        self.registry.register(Tool("integer_map", "validate extras",
            {"type": "object", "additionalProperties": {"type": "integer", "enum": [1, 2]}},
            lambda arguments, context: arguments))
        self.assertTrue((await self.call("integer_map", {"answer": 1}))["ok"])
        for arguments in ({"answer": True}, {"answer": "1"}, {"answer": 3}, {"answer": (1,)}):
            self.assertFalse((await self.call("integer_map", arguments))["ok"])
        self.registry.register(Tool("enum_bool", "enum only",
            {"type": "object", "properties": {"value": {"enum": [1]}}},
            lambda arguments, context: arguments))
        self.assertFalse((await self.call("enum_bool", {"value": True}))["ok"])
        deep = {}
        cursor = deep
        for _ in range(25):
            cursor["nested"] = {}
            cursor = cursor["nested"]
        self.assertFalse((await self.call("enum_bool", deep))["ok"])

    async def test_unknown_tool_and_missing_parameters(self):
        self.assertFalse((await self.call("shell", {"command": "ls"}))["ok"])
        self.assertFalse((await self.call("calculator", {}))["ok"])
        self.assertFalse((await self.call("calculator", {"expression": "1", "extra": 2}))["ok"])
        self.assertFalse((await self.call(["calculator"], {}))["ok"])
        self.context.max_output_chars = 128
        self.assertLessEqual(len((await self.call("unknown" * 1000, {}))["error"]), 128)

    async def test_timeout_and_cancellation(self):
        cancelled = asyncio.Event()

        async def slow(arguments, context):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        self.registry.register(Tool("slow", "timeout", {"type": "object"}, slow, timeout=.01))
        result = await self.call("slow", {})
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertTrue(cancelled.is_set())
        pending = asyncio.create_task(self.call("slow", {}))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending

    async def test_sync_handlers_run_off_event_loop(self):
        def slow_sync(arguments, context):
            time.sleep(.06)
            return "done"

        self.registry.register(Tool("slow_sync", "thread", {"type": "object"}, slow_sync, timeout=.01))
        result = await self.call("slow_sync", {})
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])

    async def test_output_bound_includes_escape_overhead(self):
        self.context.max_output_chars = 256
        self.registry.register(Tool("long", "big output", {"type": "object"},
                                    lambda args, context: '"\n\\学' * 1000))
        result = await self.call("long", {})
        self.assertTrue(result["ok"])
        self.assertTrue(result["value"]["truncated"])
        self.assertLessEqual(len(json.dumps(result["value"], ensure_ascii=False)), 256)
        self.assertGreater(result["value"]["original_chars"], 256)

    async def test_memory_context_and_confirmation(self):
        class Memory:
            def __init__(self):
                self.data = {}

            def search(self, query, limit=5):
                return [{"content": query, "limit": limit}]

            def remember(self, session, key, value):
                self.data[(session, key)] = value

            def recall(self, session, query):
                return [{"key": key, "value": value} for (owner, key), value in self.data.items()
                        if owner == session and query in key]

        self.context.memory = Memory()
        self.context.session_id = "session-a"
        self.assertFalse((await self.call("remember", {"key": "language", "value": "Python"}))["ok"])
        self.assertTrue((await self.call("remember", {"key": "language", "value": "Python"}, True))["ok"])
        self.assertEqual((await self.call("recall", {"query": "language"}))["value"],
                         [{"key": "language", "value": "Python"}])
        self.context.session_id = "session-b"
        self.assertEqual((await self.call("recall", {"query": ""}))["value"], [])
        self.assertEqual((await self.call("search_knowledge", {"query": "agent"}))["value"][0]["limit"], 5)

    async def test_definition_mutation_cannot_bypass_policy(self):
        definitions = self.registry.definitions()
        definitions[0]["function"]["parameters"]["additionalProperties"] = True
        tool = self.registry.get("write_file")
        tool.risk = "read"
        self.assertTrue(self.registry.requires_approval(ToolCall("write_file", {})))
        self.assertFalse((await self.call("calculator", {"expression": "1", "extra": 1}))["ok"])

    def test_invalid_registration(self):
        for schema in ({"type": "object", "oneOf": []}, {"type": "array"},
                       {"type": "object", "properties": {"x": {"type": ["string"]}}}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                ToolRegistry().register(Tool("invalid", "", schema, lambda a, c: a))
        with self.assertRaises(ValueError):
            self.registry.register(self.registry.get("calculator"))
        for timeout in (0, -1, float("nan"), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                ToolRegistry().register(Tool("invalid", "", {"type": "object"}, lambda a, c: a, timeout=timeout))

    def test_public_schema_validator(self):
        schema = {"type": "array", "items": {"type": "object", "properties": {
            "task": {"type": "string"}}, "required": ["task"], "additionalProperties": False}}
        self.assertIsNone(validate_schema([{"task": "学习工具调用"}], schema))
        with self.assertRaises(ValueError):
            validate_schema([{"wrong": "missing task"}], schema)
        with self.assertRaises(ValueError):
            validate_schema("abc", {"type": "string", "pattern": "[a-z]+"})


if __name__ == "__main__":
    unittest.main()
