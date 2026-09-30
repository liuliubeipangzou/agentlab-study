import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentlab.storage import SQLiteStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.store = SQLiteStore(self.root / "state.db")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_checkpoint_replacement_is_atomic_and_survives_reopen(self):
        state = {"status": "paused", "messages": [{"content": "你好"}], "obsolete": True}
        self.store.save_session("session", state)
        with self.assertRaises(ValueError):
            self.store.save_session("session", {"status": "bad", "value": float("nan")})
        self.assertEqual(self.store.load_session("session"), state)
        final = {"status": "completed", "messages": []}
        self.store.save_session("session", final)
        with SQLiteStore(self.root / "state.db") as reopened:
            self.assertEqual(reopened.load_session("session"), final)
            self.assertEqual(reopened.list_sessions()[0]["status"], "completed")
            self.assertIsNone(reopened.load_session("missing"))

    def test_events_memory_and_delete_are_session_scoped(self):
        for session in ("one", "two"):
            self.store.save_session(session, {"status": "ready"})
            self.store.remember(session, "偏好", {"language": "Python"})
            self.store.append_event(session, {"kind": "start"})
        self.store.append_event("one", {"kind": "end"})
        self.store.remember("one", "偏好", {"language": "中文"})
        self.assertEqual([event["kind"] for event in self.store.events("one")], ["start", "end"])
        self.assertEqual(self.store.recall("one", "中文")[0]["value"], {"language": "中文"})
        self.assertEqual(self.store.recall("one", "python"), [])
        self.assertEqual(len(self.store.recall("two", "PYTHON")), 1)
        self.store.acquire_session("one", "owner")
        self.store.delete_session("one")
        self.assertIsNone(self.store.load_session("one"))
        self.assertEqual(self.store.events("one"), [])
        self.assertEqual(self.store.recall("one"), [])
        self.assertEqual(len(self.store.events("two")), 1)
        self.assertTrue(self.store.acquire_session("one", "new-owner"))

    def test_concurrent_events_do_not_lose_writes(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda number: self.store.append_event("one", {"number": number}), range(120)))
        events = self.store.events("one")
        self.assertEqual(len(events), 120)
        self.assertEqual({event["number"] for event in events}, set(range(120)))

    def test_leases_are_exclusive_renewable_expiring_and_owner_scoped(self):
        with SQLiteStore(self.root / "state.db") as other:
            with patch("agentlab.storage.time.time", return_value=1000):
                self.assertTrue(self.store.acquire_session("one", "a", ttl=10))
                self.assertFalse(other.acquire_session("one", "b", ttl=10))
                other.release_session("one", "b")
                self.assertFalse(other.acquire_session("one", "b", ttl=10))
            with patch("agentlab.storage.time.time", return_value=1008):
                self.assertTrue(self.store.acquire_session("one", "a", ttl=10))
            with patch("agentlab.storage.time.time", return_value=1011):
                self.assertFalse(other.acquire_session("one", "b", ttl=10))
            with patch("agentlab.storage.time.time", return_value=1018):
                self.assertTrue(other.acquire_session("one", "b", ttl=10))
                self.store.release_session("one", "a")
                self.assertFalse(self.store.acquire_session("one", "a", ttl=10))
                other.release_session("one", "b")
                self.assertTrue(self.store.acquire_session("one", "a", ttl=10))

    def test_concurrent_connections_get_one_lease_winner(self):
        def acquire(owner):
            with SQLiteStore(self.root / "state.db") as store:
                return store.acquire_session("race", owner)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(acquire, ["a", "b", "c", "d", "e"]))
        self.assertEqual(sum(results), 1)

    def test_english_cjk_retrieval_includes_citations(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        (knowledge / "agent.md").write_text("Agent memory stores observations. Agent tools execute actions.", encoding="utf-8")
        (knowledge / "risk.txt").write_text("风控系统通过交易特征判断风险。知识检索帮助智能体引用可靠资料。", encoding="utf-8")
        (knowledge / "ignored.py").write_text("irrelevant", encoding="utf-8")
        result = self.store.ingest(knowledge)
        self.assertEqual(result["documents"], 2)
        self.assertEqual(result["chunks"], 2)
        english = self.store.search("agent memory")
        self.assertEqual(Path(english[0]["source"]).name, "agent.md")
        chinese = self.store.search("知识检索")
        self.assertEqual(Path(chinese[0]["source"]).name, "risk.txt")
        self.assertEqual(chinese[0]["chunk"], 1)
        self.assertGreater(chinese[0]["score"], 0)
        self.assertIn("知识检索", chinese[0]["text"])
        self.assertEqual(self.store.search("无关词"), [])
        self.assertEqual(self.store.search("  !!!  "), [])

    def test_reingest_replaces_stale_chunks_and_removes_missing_documents(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        document = knowledge / "guide.md"
        document.write_text("obsolete " * 240, encoding="utf-8")
        first = self.store.ingest(knowledge)
        self.assertGreater(first["chunks"], 1)
        again = self.store.ingest(knowledge)
        self.assertEqual(again["unchanged"], 1)
        self.assertEqual(len(self.store.search("obsolete", limit=100)), first["chunks"])
        document.write_text("replacement material", encoding="utf-8")
        updated = self.store.ingest(knowledge)
        self.assertEqual(updated["updated"], 1)
        self.assertEqual(updated["chunks"], 1)
        self.assertEqual(self.store.search("obsolete"), [])
        self.assertEqual(len(self.store.search("replacement")), 1)
        document.unlink()
        self.assertEqual(self.store.ingest(knowledge)["removed"], 1)
        self.assertEqual(self.store.search("replacement"), [])

    def test_overlapping_ingestion_roots_do_not_duplicate_results(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        document = knowledge / "guide.md"
        document.write_text("durable memory", encoding="utf-8")
        self.store.ingest(knowledge)
        self.store.ingest(document)
        self.assertEqual(len(self.store.search("durable")), 1)
        document.unlink()
        # The separate single-file collection still owns its indexed snapshot.
        self.assertEqual(self.store.ingest(knowledge)["removed"], 0)
        self.assertEqual(len(self.store.search("durable")), 1)

    def test_failed_ingest_keeps_previous_index(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        first = knowledge / "first.md"
        first.write_text("original material", encoding="utf-8")
        self.store.ingest(knowledge)
        first.write_text("modified material", encoding="utf-8")
        (knowledge / "bad.txt").write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            self.store.ingest(knowledge)
        self.assertEqual(len(self.store.search("original")), 1)
        self.assertEqual(self.store.search("modified"), [])

    def test_sql_failure_rolls_back_entire_ingestion(self):
        knowledge = self.root / "knowledge"
        knowledge.mkdir()
        document = knowledge / "guide.md"
        document.write_text("original", encoding="utf-8")
        self.store.ingest(knowledge)
        document.write_text("replacement", encoding="utf-8")
        # Inject a real database failure after the old chunks have been removed.
        self.store._connection.execute("""
            CREATE TRIGGER reject_new_chunks BEFORE INSERT ON chunks
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
        """)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest(knowledge)
        self.assertEqual(len(self.store.search("original")), 1)
        self.assertEqual(self.store.search("replacement"), [])
        self.store._connection.execute("DROP TRIGGER reject_new_chunks")
        self.assertEqual(self.store.ingest(knowledge)["updated"], 1)

    def test_ingestion_bounds_and_symlinks(self):
        document = self.root / "large.txt"
        document.write_text("a" * 32, encoding="utf-8")
        with patch.object(self.store, "MAX_FILE_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.store.ingest(document)
        linked = self.root / "linked.md"
        linked.symlink_to(document)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.store.ingest(linked)
        with self.assertRaises(ValueError):
            self.store.search("x", limit=0)
        with self.assertRaises(ValueError):
            self.store.acquire_session("a", "b", ttl=0)


if __name__ == "__main__":
    unittest.main()
