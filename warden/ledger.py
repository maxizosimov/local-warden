"""Append-only, hash-chained audit ledger.

Every action the agent takes (or is blocked from taking) is written here.
Each row stores the SHA-256 of the previous row, so any edit or deletion
breaks the chain and `verify()` will report exactly where.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                actor TEXT NOT NULL,
                data TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
            """
        )
        self.db.commit()

    @staticmethod
    def _hash(ts: float, kind: str, actor: str, data: str, prev: str) -> str:
        payload = json.dumps([ts, kind, actor, data, prev], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def append(self, kind: str, data: dict[str, Any], actor: str = "agent") -> dict:
        with self._lock:
            row = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            ts = time.time()
            blob = json.dumps(data, sort_keys=True, default=str)
            h = self._hash(ts, kind, actor, blob, prev)
            cur = self.db.execute(
                "INSERT INTO events (ts, kind, actor, data, prev_hash, hash) VALUES (?,?,?,?,?,?)",
                (ts, kind, actor, blob, prev, h),
            )
            self.db.commit()
            return {"id": cur.lastrowid, "ts": ts, "kind": kind, "actor": actor, "data": data, "hash": h}

    def list(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", []
        if kind:
            q += " WHERE kind = ?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in self.db.execute(q, args).fetchall()
        ]

    def verify(self) -> dict:
        prev = GENESIS
        n = 0
        for r in self.db.execute("SELECT * FROM events ORDER BY id ASC"):
            n += 1
            if r["prev_hash"] != prev:
                return {"ok": False, "broken_at": r["id"], "reason": "prev_hash mismatch", "checked": n}
            if self._hash(r["ts"], r["kind"], r["actor"], r["data"], r["prev_hash"]) != r["hash"]:
                return {"ok": False, "broken_at": r["id"], "reason": "content altered", "checked": n}
            prev = r["hash"]
        return {"ok": True, "checked": n, "head": prev}
