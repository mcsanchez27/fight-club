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
    franchise TEXT,
    retrieval_status TEXT,
    voided INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_ruling_id) REFERENCES rulings(id)
);

CREATE TABLE IF NOT EXISTS docket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    matchup TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruling_id INTEGER NOT NULL,
    claim TEXT NOT NULL,
    source_url TEXT,
    locator TEXT,
    snippet TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT,
    kind TEXT NOT NULL DEFAULT 'receipt',
    FOREIGN KEY (ruling_id) REFERENCES rulings(id)
);

CREATE TABLE IF NOT EXISTS rejudge_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruling_id INTEGER NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    FOREIGN KEY (ruling_id) REFERENCES rulings(id)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    estimated_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rulings_guild_created
    ON rulings(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rulings_message
    ON rulings(message_id);
CREATE INDEX IF NOT EXISTS idx_docket_guild
    ON docket(guild_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_citations_ruling
    ON citations(ruling_id);
CREATE INDEX IF NOT EXISTS idx_rejudge_status
    ON rejudge_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_month
    ON usage_events(month);
"""


class CourtDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced in Phase 3 to existing DBs."""
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(rulings)").fetchall()
        }
        if "franchise" not in cols:
            self._conn.execute("ALTER TABLE rulings ADD COLUMN franchise TEXT")
        if "retrieval_status" not in cols:
            self._conn.execute("ALTER TABLE rulings ADD COLUMN retrieval_status TEXT")
        if "voided" not in cols:
            self._conn.execute(
                "ALTER TABLE rulings ADD COLUMN voided INTEGER NOT NULL DEFAULT 0"
            )

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
        franchise: str | None = None,
        retrieval_status: str | None = None,
        voided: bool = False,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO rulings (
                message_id, channel_id, guild_id,
                fighter_a, fighter_b, context, verdict, parent_ruling_id,
                franchise, retrieval_status, voided
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                franchise,
                retrieval_status,
                1 if voided else 0,
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

    def insert_citation(
        self,
        *,
        ruling_id: int,
        claim: str,
        source_url: str | None,
        locator: str | None,
        snippet: str | None,
        verified: bool,
        retrieved_at: str | None,
        kind: str = "receipt",
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO citations (
                ruling_id, claim, source_url, locator, snippet,
                verified, retrieved_at, kind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ruling_id,
                claim,
                source_url,
                locator,
                snippet,
                1 if verified else 0,
                retrieved_at,
                kind,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_citations(self, ruling_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM citations WHERE ruling_id = ? ORDER BY id ASC",
            (ruling_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["verified"] = bool(d["verified"])
            out.append(d)
        return out

    def enqueue_rejudge(self, ruling_id: int, reason: str) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO rejudge_queue (ruling_id, reason, status)
            VALUES (?, ?, 'pending')
            ON CONFLICT(ruling_id) DO UPDATE SET
                reason=excluded.reason,
                status='pending',
                processed_at=NULL
            """,
            (ruling_id, reason),
        )
        self._conn.execute(
            "UPDATE rulings SET voided = 1 WHERE id = ?", (ruling_id,)
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_pending_rejudges(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT q.*, r.fighter_a, r.fighter_b, r.context, r.franchise,
                   r.channel_id, r.guild_id, r.message_id, r.verdict
            FROM rejudge_queue q
            JOIN rulings r ON r.id = q.ruling_id
            WHERE q.status = 'pending'
            ORDER BY q.created_at ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if isinstance(d.get("verdict"), str):
                d["verdict"] = json.loads(d["verdict"])
            out.append(d)
        return out

    def mark_rejudge_done(self, ruling_id: int, status: str = "done") -> None:
        self._conn.execute(
            """
            UPDATE rejudge_queue
            SET status = ?, processed_at = datetime('now')
            WHERE ruling_id = ?
            """,
            (status, ruling_id),
        )
        if status == "done":
            self._conn.execute(
                "UPDATE rulings SET voided = 0 WHERE id = ?", (ruling_id,)
            )
        self._conn.commit()

    def record_usage(
        self,
        *,
        month: str,
        tokens_in: int,
        tokens_out: int,
        estimated_usd: float,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO usage_events (month, tokens_in, tokens_out, estimated_usd)
            VALUES (?, ?, ?, ?)
            """,
            (month, tokens_in, tokens_out, estimated_usd),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def month_spend_usd(self, month: str) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(estimated_usd), 0) AS s FROM usage_events WHERE month = ?",
            (month,),
        ).fetchone()
        return float(row["s"] if row else 0.0)

    @staticmethod
    def _row_to_ruling(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["verdict"] = json.loads(d["verdict"])
        d["voided"] = bool(d.get("voided") or 0)
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
