"""SQLite court records (data/court.db)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "court.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rulings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    channel_id INTEGER,
    guild_id INTEGER,
    fighter_a TEXT NOT NULL,
    fighter_b TEXT NOT NULL,
    context TEXT,
    verdict TEXT NOT NULL,
    parent_ruling_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (parent_ruling_id) REFERENCES rulings(id)
);

CREATE TABLE IF NOT EXISTS docket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    matchup TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rulings_guild_created
    ON rulings(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rulings_message
    ON rulings(message_id);
CREATE INDEX IF NOT EXISTS idx_docket_guild
    ON docket(guild_id, created_at DESC);
"""


class CourtDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def insert_ruling(
        self,
        *,
        message_id: int | None,
        channel_id: int | None,
        guild_id: int | None,
        fighter_a: str,
        fighter_b: str,
        context: str | None,
        verdict: dict[str, Any],
        parent_ruling_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO rulings (
                message_id, channel_id, guild_id,
                fighter_a, fighter_b, context, verdict, parent_ruling_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                channel_id,
                guild_id,
                fighter_a,
                fighter_b,
                context,
                json.dumps(verdict),
                parent_ruling_id,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_ruling_by_message_id(self, message_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM rulings WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        return self._row_to_ruling(row) if row else None

    def get_ruling_by_id(self, ruling_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM rulings WHERE id = ?", (ruling_id,)
        ).fetchone()
        return self._row_to_ruling(row) if row else None

    def list_guild_rulings(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM rulings
            WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [self._row_to_ruling(r) for r in rows]

    def add_docket(
        self, guild_id: int, matchup: str, notes: str | None = None
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO docket (guild_id, matchup, notes) VALUES (?, ?, ?)",
            (guild_id, matchup, notes),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_docket(self, guild_id: int, limit: int = 25) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT * FROM docket
            WHERE guild_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (guild_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _row_to_ruling(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["verdict"] = json.loads(d["verdict"])
        return d


_db: CourtDB | None = None


def get_db(path: Path | str | None = None) -> CourtDB:
    global _db
    if path is not None:
        return CourtDB(path)
    if _db is None:
        _db = CourtDB()
    return _db


def reset_db_singleton() -> None:
    global _db
    if _db is not None:
        _db.close()
    _db = None
