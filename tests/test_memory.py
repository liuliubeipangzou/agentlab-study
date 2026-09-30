"""跨会话长期记忆检索的测试。

覆盖 BM25 排序、噪声抑制、会话排除、以及"记忆是用户私有数据"的隔离语义。
"""

import json
import tempfile
import unittest

from agentlab.agent import Agent
from agentlab.providers import ScriptedProvider
from agentlab.storage import (MEMORY_MIN_COVERAGE, MEMORY_RELATIVE_FLOOR,
                             SQLiteStore)
from agentlab.tools import ToolContext, create_builtin_tools
from agentlab.types import ModelResponse, ToolCall


class SearchMemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.remember("alpha", "goal", "掌握 Agent 执行循环与工具调用")
        self.store.remember("alpha", "lang", "偏好中文回答")
        self.store.remember("beta", "python_version", "使用 Python 3.13 的新特性")
        self.store.remember("gamma", "coffee", "喜欢燕麦拿铁")
        self.store.remember("delta", "检索笔记", "BM25 词法检索不等价于语义向量检索")

    def keys(self, query, **kwargs):
        return [item["key"] for item in self.store.search_memories(query, **kwargs)]

    def test_finds_memory_from_another_session(self):
        results = self.store.search_memories("Python")
        self.assertEqual([r["key"] for r in results], ["python_version"])
        self.assertEqual(results[0]["session_id"], "beta")

    def test_ranks_best_match_first(self):
        results = self.store.search_memories("Agent 工具调用")
        self.assertTrue(results)
        self.assertEqual(results[0]["key"], "goal")
        self.assertEqual(results[0]["session_id"], "alpha")

    def test_chinese_bigram_matching_works(self):
        self.assertIn("检索笔记", self.keys("词法"))

    def test_results_carry_citation_fields(self):
        result = self.store.search_memories("燕麦拿铁")[0]
        for field in ("session_id", "key", "value", "updated_at", "score"):
            self.assertIn(field, result)
        self.assertGreater(result["score"], 0)

    def test_irrelevant_query_returns_nothing(self):
        """单个汉字偶然重叠不应产生噪声命中。"""
        for query in ("量子纠缠股票", "天气怎么样", "马拉松训练计划", "股票基金行情"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search_memories(query), [])

    def test_returned_scores_respect_relative_floor(self):
        """返回的条目不应远低于最佳命中，否则就是偶然重叠。"""
        results = self.store.search_memories("工具调用")
        self.assertTrue(results)
        best = max(item["score"] for item in results)
        for item in results:
            self.assertGreaterEqual(item["score"], best * MEMORY_RELATIVE_FLOOR)

    def test_constants_are_sane(self):
        self.assertTrue(0 < MEMORY_RELATIVE_FLOOR <= 1)
        self.assertTrue(0 < MEMORY_MIN_COVERAGE <= 1)

    def test_exclude_current_session(self):
        self.assertEqual(self.store.search_memories("Agent", exclude_session="alpha"), [])
        self.assertEqual(self.keys("Agent"), ["goal"])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.store.search_memories(""), [])
        self.assertEqual(self.store.search_memories("   "), [])

    def test_limit_validation(self):
        for limit in (0, -1, 101, "5", None, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.store.search_memories("Agent", limit=limit)

    def test_limit_is_respected(self):
        self.store.remember("epsilon", "goal", "Agent 工具调用 与 记忆")
        self.assertLessEqual(len(self.store.search_memories("Agent", limit=1)), 1)

    def test_newer_memory_wins_on_tie(self):
        self.store.remember("zeta", "copy", "Python")
        results = self.store.search_memories("Python")
        # 同分时较新的条目排在前面。
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["key"], "copy")

    def test_list_memories_spans_sessions(self):
        listed = self.store.list_memories()
        self.assertEqual(len(listed), 5)
        self.assertEqual({item["session_id"] for item in listed},
                         {"alpha", "beta", "gamma", "delta"})

    def test_list_memories_validates_limit(self):
        for limit in (0, 1001, "10"):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.store.list_memories(limit=limit)


class SearchMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def _execute(self, arguments):
        registry = create_builtin_tools()
        context = ToolContext(workspace=tempfile.gettempdir(), session_id="current")
        return await registry.execute(ToolCall("search_memory", arguments), context, approved=True)

    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.addCleanup(self.store.close)
        self.store.remember("other-session", "goal", "掌握 Agent 执行循环")

    async def test_tool_is_registered_and_returns_cross_session_hits(self):
        registry = create_builtin_tools()
        names = {definition["function"]["name"] for definition in registry.definitions()}
        self.assertIn("search_memory", names)
        # 用真实 store 执行，确认工具能读跨会话记忆。
        context = ToolContext(workspace=tempfile.gettempdir(), memory=self.store,
                              session_id="current")
        result = await registry.execute(ToolCall("search_memory", {"query": "Agent"}),
                                        context, approved=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["value"]["count"], 1)
        self.assertEqual(result["value"]["results"][0]["session_id"], "other-session")
        self.assertIn("note", result["value"])

    async def test_exclude_current_flag_is_honoured(self):
        registry = create_builtin_tools()
        self.store.remember("current", "mine", "Agent 当前会话")
        context = ToolContext(workspace=tempfile.gettempdir(), memory=self.store,
                              session_id="current")
        with_flag = await registry.execute(
            ToolCall("search_memory", {"query": "Agent", "exclude_current": True}),
            context, approved=True)
        without = await registry.execute(
            ToolCall("search_memory", {"query": "Agent"}), context, approved=True)
        sessions_with = {r["session_id"] for r in with_flag["value"]["results"]}
        sessions_without = {r["session_id"] for r in without["value"]["results"]}
        self.assertNotIn("current", sessions_with)
        self.assertIn("current", sessions_without)

    async def test_schema_rejects_bad_arguments(self):
        result = await self._execute({"query": "", "limit": 99})
        self.assertFalse(result["ok"])


class MemoryThroughAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_can_recall_from_a_previous_session(self):
        store = SQLiteStore(":memory:")
        self.addCleanup(store.close)
        store.remember("yesterday", "python_version", "使用 Python 3.13 的新特性")
        provider = ScriptedProvider([
            ModelResponse(tool_calls=[ToolCall("search_memory", {"query": "Python 版本"})]),
            ModelResponse(content="你之前提到使用 Python 3.13。"),
        ])
        with tempfile.TemporaryDirectory() as workspace:
            agent = Agent(provider, store=store, workspace=workspace)
            result = await agent.run("我之前说用什么 Python 版本？", session_id="today")
        self.assertEqual(result.status, "completed")
        messages = store.load_session("today")["messages"]
        tool_result = json.loads(next(m for m in messages if m["role"] == "tool")["content"])
        self.assertTrue(tool_result["ok"])
        self.assertEqual(tool_result["value"]["results"][0]["value"], "使用 Python 3.13 的新特性")


if __name__ == "__main__":
    unittest.main()
