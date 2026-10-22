"""Audit log: every prepared/executed/failed action, in SQLite and as a JSON
log line on stdout (so Loki/CloudWatch/Datadog pick it up)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("opsdeck.audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user TEXT NOT NULL,
    role TEXT NOT NULL,
    integration TEXT NOT NULL,
    op TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    params TEXT NOT NULL,
    detail TEXT,
    via TEXT
);
CREATE TABLE IF NOT EXISTS used_tokens (
    jti TEXT PRIMARY KEY,
    ts REAL NOT NULL
);
"""


class AuditLog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def record(self, *, user: str, role: str, integration: str, op: str, mode: str,
               status: str, params: dict, detail: str = "", via: str = "ui") -> None:
        entry = {
            "ts": time.time(), "user": user, "role": role, "integration": integration,
            "op": op, "mode": mode, "status": status, "params": params,
            "detail": detail[:2000], "via": via,
        }
        log.info(json.dumps({"audit": entry}, default=str))
        with self._lock:
            self._db.execute(
                "INSERT INTO audit (ts,user,role,integration,op,mode,status,params,detail,via)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry["ts"], user, role, integration, op, mode, status,
                 json.dumps(params, default=str), entry["detail"], via),
            )
            self._db.commit()

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts,user,role,integration,op,mode,status,params,detail,via"
                " FROM audit ORDER BY id DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
        keys = ["ts", "user", "role", "integration", "op", "mode", "status", "params", "detail", "via"]
        out = []
        for row in rows:
            item = dict(zip(keys, row, strict=True))
            item["params"] = json.loads(item["params"])
            out.append(item)
        return out

    def consume_token(self, jti: str) -> bool:
        """Mark a confirmation token as used. False if it was already used."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO used_tokens (jti, ts) VALUES (?, ?)", (jti, time.time()))
                self._db.execute("DELETE FROM used_tokens WHERE ts < ?", (time.time() - 86400,))
                self._db.commit()
                return True
            except sqlite3.IntegrityError:
                return False
