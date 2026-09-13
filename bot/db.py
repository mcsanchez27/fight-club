"""SQLite court records (data/court.db)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "court.db"

# V2 item 2 — additive schema; bump when future migrations land.
SCHEMA_VERSION = 2

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
    fight_id INTEGER,
    kind TEXT,
    judge_model TEXT,
    winner_side TEXT,
    winner_advocate_id INTEGER,
    confidence REAL,
    opening_score_a REAL,
    opening_score_b REAL,
    close_score_a REAL,
    close_score_b REAL,
    argument_quality REAL,
    gallery_vote TEXT,
    transcript_snapshot TEXT,
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
    retrieval_seconds REAL NOT NULL DEFAULT 0,
    judge_seconds REAL NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0,
    fight_id INTEGER,
    role TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    channel_id INTEGER,
    card_message_id INTEGER,
    thread_id INTEGER,
    status TEXT NOT NULL,
    instant INTEGER NOT NULL DEFAULT 0,
    open_ended INTEGER NOT NULL DEFAULT 0,
    challenger_id INTEGER,
    challengee_id INTEGER,
    advocate_a_id INTEGER,
    advocate_b_id INTEGER,
    side_a TEXT,
    side_b TEXT,
    context TEXT,
    counters_a INTEGER NOT NULL DEFAULT 0,
    counters_b INTEGER NOT NULL DEFAULT 0,
    cancel_requested_by INTEGER,
    balance_score REAL,
    balance_favored TEXT,
    balance_reason TEXT,
    balance_warned INTEGER NOT NULL DEFAULT 0,
    underdog_accepted INTEGER NOT NULL DEFAULT 0,
    franchise_a TEXT,
    franchise_b TEXT,
    retrieval_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    accepted_at TEXT,
    rest_a_at TEXT,
    rest_b_at TEXT,
    rest_deadline_at TEXT,
    ruled_at TEXT,
    archive_at TEXT,
    season_id INTEGER
);

CREATE TABLE IF NOT EXISTS exhibits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fight_id INTEGER NOT NULL,
    ruling_id INTEGER,
    message_id INTEGER,
    author_id INTEGER,
    source_role TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT,
    url TEXT,
    status TEXT NOT NULL,
    verified_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (fight_id) REFERENCES fights(id),
    FOREIGN KEY (ruling_id) REFERENCES rulings(id)
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, key)
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
CREATE INDEX IF NOT EXISTS idx_fights_guild_status
    ON fights(guild_id, status);
CREATE INDEX IF NOT EXISTS idx_fights_card_message
    ON fights(card_message_id);
CREATE INDEX IF NOT EXISTS idx_exhibits_fight
    ON exhibits(fight_id);
"""

_RULINGS_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fight_id", "INTEGER"),
    ("kind", "TEXT"),
    ("judge_model", "TEXT"),
    ("winner_side", "TEXT"),
    ("winner_advocate_id", "INTEGER"),
    ("confidence", "REAL"),
    ("opening_score_a", "REAL"),
    ("opening_score_b", "REAL"),
    ("close_score_a", "REAL"),
    ("close_score_b", "REAL"),
    ("argument_quality", "REAL"),
    ("gallery_vote", "TEXT"),
    ("transcript_snapshot", "TEXT"),
)

_USAGE_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("retrieval_seconds", "REAL NOT NULL DEFAULT 0"),
    ("judge_seconds", "REAL NOT NULL DEFAULT 0"),
    ("total_seconds", "REAL NOT NULL DEFAULT 0"),
    ("fight_id", "INTEGER"),
    ("role", "TEXT"),
)

_FIGHTS_OPTIONAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("retrieval_status", "TEXT"),
)


class CourtDB:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _table_columns(self, table: str) -> set[str]:
        return {
            r["name"]
            for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _add_columns_if_missing(
        self, table: str, columns: tuple[tuple[str, str], ...]
    ) -> None:
        existing = self._table_columns(table)
        for name, decl in columns:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _migrate(self) -> None:
        """Additive Phase 3 / V2 migrations guarded by schema_version."""
        # Phase 3 columns on pre-Phase-3 DBs (also covered by CREATE IF NOT EXISTS
        # on fresh DBs that already include them in _SCHEMA).
        self._add_columns_if_missing(
            "rulings",
            (
                ("franchise", "TEXT"),
                ("retrieval_status", "TEXT"),
                ("voided", "INTEGER NOT NULL DEFAULT 0"),
            ),
        )
        self._add_columns_if_missing("rulings", _RULINGS_V2_COLUMNS)
        self._add_columns_if_missing("usage_events", _USAGE_V2_COLUMNS)
        # fights may exist from an older partial migration without retrieval_status
        if "fights" in {
            r[0]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }:
            self._add_columns_if_missing("fights", _FIGHTS_OPTIONAL_COLUMNS)

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rulings_fight ON rulings(fight_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_fight ON usage_events(fight_id)"
        )

        row = self._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        elif int(row["version"]) < SCHEMA_VERSION:
            self._conn.execute(
                "UPDATE schema_version SET version = ?",
                (SCHEMA_VERSION,),
            )

    def close(self) -> None:
        self._conn.close()

    def get_schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        return int(row["version"]) if row else 0

    # --- fights -----------------------------------------------------------

    def create_fight(
        self,
        *,
        guild_id: int | None = None,
        channel_id: int | None = None,
        card_message_id: int | None = None,
        thread_id: int | None = None,
        status: str = "proposed",
        instant: bool = False,
        open_ended: bool = False,
        challenger_id: int | None = None,
        challengee_id: int | None = None,
        advocate_a_id: int | None = None,
        advocate_b_id: int | None = None,
        side_a: str | None = None,
        side_b: str | None = None,
        context: str | None = None,
        counters_a: int = 0,
        counters_b: int = 0,
        cancel_requested_by: int | None = None,
        balance_score: float | None = None,
        balance_favored: str | None = None,
        balance_reason: str | None = None,
        balance_warned: bool = False,
        underdog_accepted: bool = False,
        franchise_a: str | None = None,
        franchise_b: str | None = None,
        retrieval_status: str | None = None,
        expires_at: str | None = None,
        accepted_at: str | None = None,
        rest_a_at: str | None = None,
        rest_b_at: str | None = None,
        rest_deadline_at: str | None = None,
        ruled_at: str | None = None,
        archive_at: str | None = None,
        season_id: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO fights (
                guild_id, channel_id, card_message_id, thread_id,
                status, instant, open_ended,
                challenger_id, challengee_id, advocate_a_id, advocate_b_id,
                side_a, side_b, context, counters_a, counters_b,
                cancel_requested_by, balance_score, balance_favored, balance_reason,
                balance_warned, underdog_accepted, franchise_a, franchise_b,
                retrieval_status, expires_at, accepted_at, rest_a_at, rest_b_at,
                rest_deadline_at, ruled_at, archive_at, season_id
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                guild_id,
                channel_id,
                card_message_id,
                thread_id,
                status,
                1 if instant else 0,
                1 if open_ended else 0,
                challenger_id,
                challengee_id,
                advocate_a_id,
                advocate_b_id,
                side_a,
                side_b,
                context,
                counters_a,
                counters_b,
                cancel_requested_by,
                balance_score,
                balance_favored,
                balance_reason,
                1 if balance_warned else 0,
                1 if underdog_accepted else 0,
                franchise_a,
                franchise_b,
                retrieval_status,
                expires_at,
                accepted_at,
                rest_a_at,
                rest_b_at,
                rest_deadline_at,
                ruled_at,
                archive_at,
                season_id,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_fight(self, fight_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM fights WHERE id = ?", (fight_id,)
        ).fetchone()
        return self._row_to_fight(row) if row else None

    def list_fights(
        self,
        *,
        status: str | None = None,
        guild_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT * FROM fights {where} ORDER BY id ASC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_fight(r) for r in rows]

    def update_fight(self, fight_id: int, **fields: Any) -> None:
        """Patch fight columns. Bool fields are stored as 0/1."""
        if not fields:
            return
        bool_cols = {
            "instant",
            "open_ended",
            "balance_warned",
            "underdog_accepted",
        }
        cols: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            if key in bool_cols and value is not None:
                value = 1 if value else 0
            cols.append(f"{key} = ?")
            vals.append(value)
        vals.append(fight_id)
        self._conn.execute(
            f"UPDATE fights SET {', '.join(cols)} WHERE id = ?",
            vals,
        )
        self._conn.commit()

    # --- exhibits ---------------------------------------------------------

    def insert_exhibit(
        self,
        *,
        fight_id: int,
        source_role: str,
        kind: str,
        status: str,
        ruling_id: int | None = None,
        message_id: int | None = None,
        author_id: int | None = None,
        text: str | None = None,
        url: str | None = None,
        verified_url: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO exhibits (
                fight_id, ruling_id, message_id, author_id,
                source_role, kind, text, url, status, verified_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fight_id,
                ruling_id,
                message_id,
                author_id,
                source_role,
                kind,
                text,
                url,
                status,
                verified_url,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def list_exhibits(self, fight_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM exhibits WHERE fight_id = ? ORDER BY id ASC",
            (fight_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- guild_config -----------------------------------------------------

    def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        self._conn.execute(
            """
            INSERT INTO guild_config (guild_id, key, value, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(guild_id, key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now')
            """,
            (guild_id, key, value),
        )
        self._conn.commit()

    def get_guild_config(self, guild_id: int, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM guild_config WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        ).fetchone()
        return str(row["value"]) if row else None

    def list_guild_config(self, guild_id: int) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM guild_config WHERE guild_id = ? ORDER BY key",
            (guild_id,),
        ).fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    # --- rulings (V1 + V2 extensions) -------------------------------------

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
        fight_id: int | None = None,
        kind: str | None = None,
        judge_model: str | None = None,
        winner_side: str | None = None,
        winner_advocate_id: int | None = None,
        confidence: float | None = None,
        opening_score_a: float | None = None,
        opening_score_b: float | None = None,
        close_score_a: float | None = None,
        close_score_b: float | None = None,
        argument_quality: float | None = None,
        gallery_vote: str | None = None,
        transcript_snapshot: Any | None = None,
    ) -> int:
        snap = transcript_snapshot
        if snap is not None and not isinstance(snap, str):
            snap = json.dumps(snap)
        cur = self._conn.execute(
            """
            INSERT INTO rulings (
                message_id, channel_id, guild_id,
                fighter_a, fighter_b, context, verdict, parent_ruling_id,
                franchise, retrieval_status, voided,
                fight_id, kind, judge_model, winner_side, winner_advocate_id,
                confidence, opening_score_a, opening_score_b,
                close_score_a, close_score_b, argument_quality,
                gallery_vote, transcript_snapshot
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
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
                fight_id,
                kind,
                judge_model,
                winner_side,
                winner_advocate_id,
                confidence,
                opening_score_a,
                opening_score_b,
                close_score_a,
                close_score_b,
                argument_quality,
                gallery_vote,
                snap,
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
        retrieval_seconds: float = 0.0,
        judge_seconds: float = 0.0,
        total_seconds: float = 0.0,
        fight_id: int | None = None,
        role: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO usage_events (
                month, tokens_in, tokens_out, estimated_usd,
                retrieval_seconds, judge_seconds, total_seconds,
                fight_id, role
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                month,
                tokens_in,
                tokens_out,
                estimated_usd,
                float(retrieval_seconds or 0.0),
                float(judge_seconds or 0.0),
                float(total_seconds or 0.0),
                fight_id,
                role,
            ),
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
        snap = d.get("transcript_snapshot")
        if isinstance(snap, str) and snap:
            try:
                d["transcript_snapshot"] = json.loads(snap)
            except json.JSONDecodeError:
                pass
        return d

    @staticmethod
    def _row_to_fight(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        for key in ("instant", "open_ended", "balance_warned", "underdog_accepted"):
            if key in d:
                d[key] = bool(d[key] or 0)
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
