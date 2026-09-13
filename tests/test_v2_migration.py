"""V2 item 2 — additive schema migration from a V1 fixture DB."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bot.db import SCHEMA_VERSION, CourtDB

# Minimal pre-Phase-3 / V1 shape (no franchise/voided, no usage timings, no V2 tables).
_V1_SCHEMA = """
CREATE TABLE rulings (
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

CREATE TABLE docket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    matchup TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    estimated_usd REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_FIGHT_COLS = {
    "id",
    "guild_id",
    "channel_id",
    "card_message_id",
    "thread_id",
    "status",
    "instant",
    "open_ended",
    "challenger_id",
    "challengee_id",
    "advocate_a_id",
    "advocate_b_id",
    "side_a",
    "side_b",
    "context",
    "counters_a",
    "counters_b",
    "cancel_requested_by",
    "balance_score",
    "balance_favored",
    "balance_reason",
    "balance_warned",
    "underdog_accepted",
    "franchise_a",
    "franchise_b",
    "retrieval_status",
    "created_at",
    "expires_at",
    "accepted_at",
    "rest_a_at",
    "rest_b_at",
    "rest_deadline_at",
    "ruled_at",
    "archive_at",
    "season_id",
}

_EXHIBIT_COLS = {
    "id",
    "fight_id",
    "ruling_id",
    "message_id",
    "author_id",
    "source_role",
    "kind",
    "text",
    "url",
    "status",
    "verified_url",
    "created_at",
}

_RULINGS_V2_COLS = {
    "fight_id",
    "kind",
    "judge_model",
    "winner_side",
    "winner_advocate_id",
    "confidence",
    "opening_score_a",
    "opening_score_b",
    "close_score_a",
    "close_score_b",
    "argument_quality",
    "gallery_vote",
    "transcript_snapshot",
}

_USAGE_V2_COLS = {
    "fight_id",
    "role",
    "retrieval_seconds",
    "judge_seconds",
    "total_seconds",
}


def _make_v1_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_V1_SCHEMA)
    verdict = json.dumps(
        {
            "matchup": "A vs B",
            "steelman_a": "a",
            "steelman_b": "b",
            "concessions": [],
            "unknowns": [],
            "ruling": "A wins",
            "winner": "A",
            "confidence": 8,
            "citations": [],
        }
    )
    conn.execute(
        """
        INSERT INTO rulings (
            message_id, channel_id, guild_id,
            fighter_a, fighter_b, context, verdict, parent_ruling_id
        ) VALUES (101, 202, 303, 'Goku', 'Superman', 'no prep', ?, NULL)
        """,
        (verdict,),
    )
    conn.execute(
        """
        INSERT INTO usage_events (month, tokens_in, tokens_out, estimated_usd)
        VALUES ('2026-09', 100, 50, 0.01)
        """
    )
    conn.commit()
    conn.close()


def _cols(db: CourtDB, table: str) -> set[str]:
    return db._table_columns(table)


def test_migrate_v1_fixture_upgrades_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "v1_court.db"
    _make_v1_db(path)

    # Precondition: no V2 tables / schema_version
    pre = sqlite3.connect(path)
    names = {
        r[0]
        for r in pre.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "schema_version" not in names
    assert "fights" not in names
    assert "fight_id" not in {
        r[1] for r in pre.execute("PRAGMA table_info(rulings)").fetchall()
    }
    pre.close()

    db = CourtDB(path)
    assert db.get_schema_version() == SCHEMA_VERSION == 2

    names = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"fights", "exhibits", "guild_config", "schema_version", "receipts"} <= names

    assert _FIGHT_COLS <= _cols(db, "fights")
    assert _EXHIBIT_COLS <= _cols(db, "exhibits")
    assert {"guild_id", "key", "value", "updated_at"} <= _cols(db, "guild_config")
    assert _RULINGS_V2_COLS <= _cols(db, "rulings")
    assert _USAGE_V2_COLS <= _cols(db, "usage_events")

    # V1 ruling preserved; fight_id stays NULL
    ruling = db.get_ruling_by_message_id(101)
    assert ruling is not None
    assert ruling["fighter_a"] == "Goku"
    assert ruling["verdict"]["winner"] == "A"
    assert ruling["fight_id"] is None
    assert ruling["parent_ruling_id"] is None

    # V1 usage row still readable; new columns defaulted
    row = db._conn.execute(
        "SELECT tokens_in, fight_id, role, retrieval_seconds FROM usage_events"
    ).fetchone()
    assert row["tokens_in"] == 100
    assert row["fight_id"] is None
    assert row["role"] is None
    assert row["retrieval_seconds"] == 0

    # Idempotent reopen
    db.close()
    db2 = CourtDB(path)
    assert db2.get_schema_version() == 2
    assert db2.get_ruling_by_message_id(101)["fight_id"] is None
    db2.close()


def test_new_db_has_schema_version_and_v2_tables(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "fresh.db")
    assert db.get_schema_version() == 2
    assert _FIGHT_COLS <= _cols(db, "fights")
    assert "retrieval_status" in _cols(db, "fights")
    db.close()


def test_fight_exhibit_guild_config_round_trip(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "rt.db")
    fid = db.create_fight(
        guild_id=9,
        channel_id=8,
        card_message_id=7,
        status="proposed",
        challenger_id=1,
        challengee_id=2,
        advocate_a_id=1,
        advocate_b_id=2,
        side_a="Aragorn",
        side_b="Goku",
        context="no ki",
        retrieval_status="ok",
        balance_score=4.0,
        balance_favored="b",
        balance_reason="power gap",
        balance_warned=True,
        expires_at="2026-09-13T18:00:00Z",
    )
    fight = db.get_fight(fid)
    assert fight is not None
    assert fight["side_a"] == "Aragorn"
    assert fight["side_b"] == "Goku"
    assert fight["retrieval_status"] == "ok"
    assert fight["balance_warned"] is True
    assert fight["instant"] is False

    db.update_fight(fid, status="accepted", thread_id=555, accepted_at="2026-09-13T12:00:00Z")
    fight = db.get_fight(fid)
    assert fight["status"] == "accepted"
    assert fight["thread_id"] == 555

    eid = db.insert_exhibit(
        fight_id=fid,
        message_id=42,
        author_id=1,
        source_role="advocate",
        kind="link",
        text=None,
        url="https://example.test/page",
        status="verified",
        verified_url="https://example.test/page",
    )
    exhibits = db.list_exhibits(fid)
    assert len(exhibits) == 1
    assert exhibits[0]["id"] == eid
    assert exhibits[0]["kind"] == "link"
    assert exhibits[0]["source_role"] == "advocate"
    assert exhibits[0]["status"] == "verified"

    db.set_guild_config(9, "rest_timeout_hours", "24")
    db.set_guild_config(9, "daily_cap", "50")
    assert db.get_guild_config(9, "rest_timeout_hours") == "24"
    assert db.list_guild_config(9) == {
        "daily_cap": "50",
        "rest_timeout_hours": "24",
    }
    # upsert
    db.set_guild_config(9, "daily_cap", "60")
    assert db.get_guild_config(9, "daily_cap") == "60"
    db.close()


def test_ruling_v2_fields_and_nullable_capture_scores(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "rulings.db")
    fid = db.create_fight(
        guild_id=1,
        status="ruled",
        side_a="A",
        side_b="B",
        challenger_id=10,
        challengee_id=20,
        advocate_a_id=10,
        advocate_b_id=20,
    )
    rid = db.insert_ruling(
        message_id=1,
        channel_id=2,
        guild_id=1,
        fighter_a="A",
        fighter_b="B",
        context=None,
        verdict={
            "matchup": "A vs B",
            "steelman_a": "a",
            "steelman_b": "b",
            "concessions": [],
            "unknowns": [],
            "ruling": "A wins",
            "winner": "A",
            "confidence": 7,
            "citations": [],
        },
        fight_id=fid,
        kind="initial",
        judge_model="claude-sonnet-5",
        winner_side="a",
        winner_advocate_id=10,
        confidence=7.0,
        opening_score_a=None,
        opening_score_b=None,
        close_score_a=None,
        close_score_b=None,
        argument_quality=None,
        transcript_snapshot={"turns": ["[A:A] hello"]},
    )
    row = db.get_ruling_by_id(rid)
    assert row["fight_id"] == fid
    assert row["kind"] == "initial"
    assert row["winner_side"] == "a"
    assert row["opening_score_a"] is None
    assert row["argument_quality"] is None
    assert row["transcript_snapshot"]["turns"][0].startswith("[A:A]")

    # Item 1 booking still works (no fight_id/role required)
    uid = db.record_usage(
        month="2026-09",
        tokens_in=10,
        tokens_out=5,
        estimated_usd=0.01,
        retrieval_seconds=1.0,
        judge_seconds=2.0,
        total_seconds=3.0,
    )
    assert uid >= 1
    uid2 = db.record_usage(
        month="2026-09",
        tokens_in=11,
        tokens_out=6,
        estimated_usd=0.02,
        fight_id=fid,
        role="balance",
    )
    row2 = db._conn.execute(
        "SELECT fight_id, role FROM usage_events WHERE id = ?", (uid2,)
    ).fetchone()
    assert row2["fight_id"] == fid
    assert row2["role"] == "balance"
    db.close()


def test_migration_is_additive_only(tmp_path: Path) -> None:
    """Opening twice does not drop V1 columns or rewrite existing rows."""
    path = tmp_path / "additive.db"
    _make_v1_db(path)
    db = CourtDB(path)
    v1_cols = {
        "id",
        "message_id",
        "channel_id",
        "guild_id",
        "fighter_a",
        "fighter_b",
        "context",
        "verdict",
        "parent_ruling_id",
        "created_at",
    }
    assert v1_cols <= _cols(db, "rulings")
    before = db.get_ruling_by_message_id(101)
    db.close()
    db = CourtDB(path)
    after = db.get_ruling_by_message_id(101)
    assert after["id"] == before["id"]
    assert after["verdict"] == before["verdict"]
    assert after["fight_id"] is None
    db.close()
