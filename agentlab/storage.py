"""SQLite persistence and a small, inspectable lexical knowledge index.

All writes are transactions. A single store may be used from asyncio.to_thread;
separate instances coordinate through SQLite and the session lease table.
"""

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


_WORDS = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


def _tokens(text: str) -> List[str]:
    """English words plus Chinese characters and adjacent character pairs."""
    result = []
    for token in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if "\u3400" <= token[0] <= "\u9fff":
            result.extend(token)
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class SQLiteStore:
    """Persist JSON sessions, events, session memory, and cited text chunks.

    Ingestion is deliberately bounded and accepts UTF-8 .md and .txt files.
    Limits are per ingest operation; a failed operation leaves the index intact.
    """

    MAX_DOCUMENTS = 1000
    MAX_FILE_BYTES = 2 * 1024 * 1024
    MAX_TOTAL_BYTES = 32 * 1024 * 1024
    MAX_ENTRIES = 20000
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 120

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            if self.path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    event TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_session ON events(session_id, id);
                CREATE TABLE IF NOT EXISTS memories (
                    session_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(session_id, key)
                );
                CREATE TABLE IF NOT EXISTS session_leases (
                    session_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    source TEXT PRIMARY KEY, content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ingestion_memberships (
                    collection TEXT NOT NULL,
                    source TEXT NOT NULL REFERENCES documents(source) ON DELETE CASCADE,
                    PRIMARY KEY(collection, source)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    source TEXT NOT NULL REFERENCES documents(source) ON DELETE CASCADE,
                    chunk INTEGER NOT NULL, text TEXT NOT NULL, token_count INTEGER NOT NULL,
                    PRIMARY KEY(source, chunk)
                );
                CREATE TABLE IF NOT EXISTS terms (
                    term TEXT NOT NULL, source TEXT NOT NULL, chunk INTEGER NOT NULL,
                    frequency INTEGER NOT NULL, PRIMARY KEY(term, source, chunk),
                    FOREIGN KEY(source, chunk) REFERENCES chunks(source, chunk) ON DELETE CASCADE
                );
            """)

    def save_session(self, session_id: str, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise TypeError("session state must be a dictionary")
        encoded = _json(state)  # Validate before touching an existing checkpoint.
        now = _now()
        with self._lock, self._connection:
            self._connection.execute("""
                INSERT INTO sessions VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET state=excluded.state,
                    status=excluded.status, updated_at=excluded.updated_at
            """, (session_id, encoded, str(state.get("status", "unknown")), now, now))

    def load_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return json.loads(row["state"]) if row else None

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("""
                SELECT session_id, status, created_at, updated_at FROM sessions
                ORDER BY updated_at DESC, session_id
            """).fetchall()
        return [dict(row) for row in rows]

    def delete_session(self, session_id: str) -> None:
        with self._lock, self._connection:
            for table in ("events", "memories", "session_leases", "sessions"):
                self._connection.execute(
                    "DELETE FROM {} WHERE session_id=?".format(table), (session_id,)
                )

    def append_event(self, session_id: str, event: Dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise TypeError("event must be a dictionary")
        encoded = _json(event)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(session_id, event) VALUES (?, ?)", (session_id, encoded)
            )

    def events(self, session_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT event FROM events WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        return [json.loads(row["event"]) for row in rows]

    def remember(self, session_id: str, key: str, value: Any) -> None:
        encoded = _json(value)
        with self._lock, self._connection:
            self._connection.execute("""
                INSERT INTO memories VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
            """, (session_id, key, encoded, _now()))

    def recall(self, session_id: str, query: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("""
                SELECT key, value, updated_at FROM memories WHERE session_id=? ORDER BY key
            """, (session_id,)).fetchall()
        query = query.casefold()
        return [
            {"key": row["key"], "value": json.loads(row["value"]), "updated_at": row["updated_at"]}
            for row in rows
            if not query or query in (row["key"] + " " + row["value"]).casefold()
        ]

    def acquire_session(self, session_id: str, owner: str, ttl: float = 300) -> bool:
        """Acquire or renew a lease; callers must renew before a long turn expires."""
        if not owner or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("a nonempty owner and a finite positive ttl are required")
        now = time.time()
        with self._lock, self._connection:
            cursor = self._connection.execute("""
                INSERT INTO session_leases VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET owner=excluded.owner,
                    expires_at=excluded.expires_at
                WHERE session_leases.owner=excluded.owner OR session_leases.expires_at<=?
            """, (session_id, owner, now + ttl, now))
            return cursor.rowcount == 1

    def release_session(self, session_id: str, owner: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM session_leases WHERE session_id=? AND owner=?", (session_id, owner)
            )

    def _files(self, root: Path) -> List[Path]:
        if root.is_symlink():
            raise ValueError("ingestion root must not be a symbolic link")
        if not root.exists():
            raise FileNotFoundError(str(root))
        if root.is_file():
            if root.suffix.lower() not in (".md", ".txt"):
                raise ValueError("only .md and .txt files can be ingested")
            return [root.resolve()]
        if not root.is_dir():
            raise ValueError("ingestion requires a regular file or directory")
        files = []
        entries = 0

        def fail_on_walk_error(error: OSError) -> None:
            # An incomplete scan must not delete previously indexed documents.
            raise error

        for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail_on_walk_error):
            entries += len(dirs) + len(names)
            if entries > self.MAX_ENTRIES:
                raise ValueError("ingestion exceeds directory entry limit")
            dirs[:] = sorted(
                name for name in dirs
                if not name.startswith(".") and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if (name.startswith(".") or path.is_symlink() or not path.is_file()
                        or path.suffix.lower() not in (".md", ".txt")):
                    continue
                files.append(path.resolve())
                if len(files) > self.MAX_DOCUMENTS:
                    raise ValueError("ingestion exceeds document limit")
        return files

    def _chunks(self, text: str) -> List[str]:
        text = text.strip()
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.CHUNK_SIZE, len(text))
            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)
            if end == len(text):
                break
            start = end - self.CHUNK_OVERLAP
        return chunks

    def ingest(self, path: Path) -> Dict[str, int]:
        """Atomically reconcile one file/directory with its current source files.

        Reingesting unchanged files is a no-op. Updated files replace old chunks.
        Removed files are dropped if no other ingestion root still references them.
        """
        root = Path(path).expanduser()
        files = self._files(root)
        collection = str(root.resolve())
        prepared = []
        total_bytes = 0
        for file in files:
            with file.open("rb") as stream:
                raw = stream.read(self.MAX_FILE_BYTES + 1)
            if len(raw) > self.MAX_FILE_BYTES:
                raise ValueError("file exceeds ingestion byte limit: {}".format(file))
            total_bytes += len(raw)
            if total_bytes > self.MAX_TOTAL_BYTES:
                raise ValueError("ingestion exceeds total byte limit")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("knowledge files must be UTF-8: {}".format(file)) from exc
            prepared.append((str(file), hashlib.sha256(raw).hexdigest(), self._chunks(text)))
        result = {"documents": len(prepared), "chunks": 0, "added": 0,
                  "updated": 0, "unchanged": 0, "removed": 0}
        with self._lock, self._connection:
            previous = {row["source"] for row in self._connection.execute(
                "SELECT source FROM ingestion_memberships WHERE collection=?", (collection,)
            )}
            self._connection.execute("DELETE FROM ingestion_memberships WHERE collection=?", (collection,))
            for source, digest, chunks in prepared:
                old = self._connection.execute(
                    "SELECT content_hash FROM documents WHERE source=?", (source,)
                ).fetchone()
                if old and old["content_hash"] == digest:
                    result["unchanged"] += 1
                else:
                    result["updated" if old else "added"] += 1
                    self._connection.execute("""
                        INSERT INTO documents VALUES (?, ?, ?)
                        ON CONFLICT(source) DO UPDATE SET content_hash=excluded.content_hash,
                            indexed_at=excluded.indexed_at
                    """, (source, digest, _now()))
                    self._connection.execute("DELETE FROM chunks WHERE source=?", (source,))
                    for number, text in enumerate(chunks, start=1):
                        frequencies = Counter(_tokens(text))
                        self._connection.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)",
                                                 (source, number, text, sum(frequencies.values())))
                        self._connection.executemany("INSERT INTO terms VALUES (?, ?, ?, ?)",
                            [(term, source, number, count) for term, count in frequencies.items()])
                self._connection.execute("INSERT INTO ingestion_memberships VALUES (?, ?)",
                                         (collection, source))
                result["chunks"] += len(chunks)
            current = {item[0] for item in prepared}
            for source in previous - current:
                cursor = self._connection.execute("""
                    DELETE FROM documents WHERE source=? AND NOT EXISTS (
                        SELECT 1 FROM ingestion_memberships WHERE source=?
                    )
                """, (source, source))
                result["removed"] += cursor.rowcount
        return result

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Rank matching chunks with BM25; source and chunk form a citation."""
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError("search limit must be an integer between 1 and 100")
        query_terms = list(dict.fromkeys(_tokens(query)))[:64]
        if not query_terms:
            return []
        placeholders = ",".join("?" for _ in query_terms)
        with self._lock, self._connection:
            # Multiple SELECTs must see one snapshot even if another process ingests.
            self._connection.execute("BEGIN")
            stats = self._connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(AVG(token_count), 0) AS avg FROM chunks"
            ).fetchone()
            if not stats["n"]:
                return []
            frequencies = {row["term"]: row["df"] for row in self._connection.execute(
                "SELECT term, COUNT(*) AS df FROM terms WHERE term IN ({}) GROUP BY term".format(placeholders),
                query_terms,
            )}
            rows = self._connection.execute("""
                SELECT t.term, t.frequency, c.source, c.chunk, c.text, c.token_count
                FROM terms t JOIN chunks c ON t.source=c.source AND t.chunk=c.chunk
                WHERE t.term IN ({})
            """.format(placeholders), query_terms).fetchall()
        hits = {}
        average = max(float(stats["avg"]), 1.0)
        for row in rows:
            key = (row["source"], row["chunk"])
            if key not in hits:
                hits[key] = {"source": row["source"], "chunk": row["chunk"],
                             "text": row["text"], "score": 0.0}
            df = frequencies[row["term"]]
            idf = math.log(1 + (stats["n"] - df + 0.5) / (df + 0.5))
            tf = row["frequency"]
            denominator = tf + 1.5 * (0.25 + 0.75 * row["token_count"] / average)
            hits[key]["score"] += idf * tf * 2.5 / denominator
        ranked = sorted(hits.values(), key=lambda row: (-row["score"], row["source"], row["chunk"]))
        for hit in ranked:
            hit["score"] = round(hit["score"], 6)
        return ranked[:limit]

    def knowledge_stats(self) -> Dict[str, int]:
        with self._lock:
            row = self._connection.execute("""
                SELECT (SELECT COUNT(*) FROM documents) AS documents,
                       (SELECT COUNT(*) FROM chunks) AS chunks
            """).fetchone()
        return dict(row)

    def list_documents(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute("""
                SELECT d.source, COUNT(c.chunk) AS chunks
                FROM documents d LEFT JOIN chunks c ON d.source=c.source
                GROUP BY d.source ORDER BY d.source
            """).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
