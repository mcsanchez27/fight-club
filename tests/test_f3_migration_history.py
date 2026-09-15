"""F3 — migration verified against the *real* historical schema, from git.

NOTES 71: ``test_v2_migration.py`` builds its V1 fixture from hand-written
``CREATE TABLE`` statements, so it asserts that *that fixture* migrates — it
would stay green even if the shipped V1 schema had drifted from it. These tests
close that gap: they extract the actual ``bot/db.py`` at two historical commits,
build databases with that code, then open them with the current ``CourtDB``.

  a2b4754 — true V1 (rulings + docket only)
  2ea808b — Phase 3 + item 1, the immediate pre-V2 shape

Skips rather than fails when the git objects are unavailable (shallow clone or
source tarball), since the check is about history, not the working tree.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from bot.db import SCHEMA_VERSION, CourtDB

REPO_ROOT = Path(__file__).resolve().parents[1]

# Oldest first.
HISTORICAL_COMMITS = [
    pytest.param("a2b4754", id="a2b4754-true-v1"),
    pytest.param("2ea808b", id="2ea808b-pre-v2"),
]


def _git_show(commit: str, path: str) -> str | None:
    """``git show <commit>:<path>``, or None when git/the object is unavailable."""
    try:
        proc = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _load_historical_db_module(commit: str, tmp_path: Path) -> Any:
    """Import the historical ``bot/db.py`` standalone (both versions are stdlib-only)."""
    src = _git_show(commit, "bot/db.py")
    if src is None:
        pytest.skip(f"git object {commit}:bot/db.py unavailable (shallow clone?)")

    mod_path = tmp_path / f"historical_db_{commit}.py"
    mod_path.write_text(src, encoding="utf-8")
    mod_name = f"_historical_db_{commit}"

    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(mod_name, None)
        raise
    return module


def _seed_with_historical_code(module: Any, path: Path) -> dict[str, Any]:
    """Write rows using the historical API — whatever that commit actually had."""
    db = module.CourtDB(path)
    ruling_id = db.insert_ruling(
        message_id=101,
        channel_id=202,
        guild_id=303,
        fighter_a="Goku",
        fighter_b="Superman",
        context="no prep, open field",
        verdict={"winner": "A", "confidence": 7, "reasoning": "speed blitz"},
    )
    db.add_docket(303, "Batman vs Iron Man", "banked")

    seeded: dict[str, Any] = {"ruling_id": ruling_id, "citations": 0, "usage": 0}

    if hasattr(db, "insert_citation"):
        db.insert_citation(
            ruling_id=ruling_id,
            claim="Goku learned Instant Transmission on Yardrat",
            source_url="https://dragonball.fandom.com/wiki/Instant_Transmission",
            locator="Instant Transmission",
            snippet="technique learned on Yardrat",
            verified=True,
            retrieved_at="2026-01-01T00:00:00+00:00",
        )
        seeded["citations"] = 1
    if hasattr(db, "enqueue_rejudge"):
        db.enqueue_rejudge(ruling_id, "unavailable")
    if hasattr(db, "record_usage"):
        db.record_usage(
            month="2026-01", tokens_in=100, tokens_out=50, estimated_usd=0.0125
        )
        seeded["usage"] = 1

    db.close()
    return seeded


def _raw_table(path: Path, table: str) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _table_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


@pytest.mark.parametrize("commit", HISTORICAL_COMMITS)
def test_historical_db_reaches_schema_version_2(commit: str, tmp_path: Path) -> None:
    module = _load_historical_db_module(commit, tmp_path)
    path = tmp_path / f"court_{commit}.db"
    _seed_with_historical_code(module, path)

    # Precondition: the historical code really did produce a pre-V2 database.
    before = _table_names(path)
    assert "fights" not in before
    assert "exhibits" not in before
    assert "guild_config" not in before

    db = CourtDB(path)
    try:
        assert db.get_schema_version() == SCHEMA_VERSION == 2
        after = _table_names(path)
        assert {"fights", "exhibits", "guild_config", "schema_version"} <= after
        # Migration is additive — nothing the historical schema had may vanish.
        assert before <= after
    finally:
        db.close()


@pytest.mark.parametrize("commit", HISTORICAL_COMMITS)
def test_historical_rows_survive_byte_for_byte(commit: str, tmp_path: Path) -> None:
    """Every column value the old code wrote reads back identically after migration."""
    module = _load_historical_db_module(commit, tmp_path)
    path = tmp_path / f"court_{commit}.db"
    seeded = _seed_with_historical_code(module, path)

    before = {t: _raw_table(path, t) for t in ("rulings", "docket")}
    if seeded["citations"]:
        before["citations"] = _raw_table(path, "citations")
    if seeded["usage"]:
        before["usage_events"] = _raw_table(path, "usage_events")

    CourtDB(path).close()  # migrate

    for table, old_rows in before.items():
        new_rows = _raw_table(path, table)
        assert len(new_rows) == len(old_rows), f"{table} lost or gained rows"
        for old, new in zip(old_rows, new_rows):
            for column, value in old.items():
                assert new[column] == value, (
                    f"{table}.{column} changed during migration: "
                    f"{value!r} -> {new[column]!r}"
                )


@pytest.mark.parametrize("commit", HISTORICAL_COMMITS)
def test_historical_rows_readable_through_current_api(
    commit: str, tmp_path: Path
) -> None:
    """V1 accessors still resolve the old rows after the V2 columns land."""
    module = _load_historical_db_module(commit, tmp_path)
    path = tmp_path / f"court_{commit}.db"
    _seed_with_historical_code(module, path)

    db = CourtDB(path)
    try:
        ruling = db.get_ruling_by_message_id(101)
        assert ruling is not None
        assert ruling["fighter_a"] == "Goku"
        assert ruling["fighter_b"] == "Superman"
        assert ruling["verdict"]["winner"] == "A"
        assert ruling["verdict"]["confidence"] == 7
        # V2 columns exist on the old row but stay empty.
        assert ruling["fight_id"] is None
        assert ruling["parent_ruling_id"] is None

        listed = db.list_guild_rulings(303, limit=10)
        assert [r["id"] for r in listed] == [ruling["id"]]
        assert [d["matchup"] for d in db.list_docket(303)] == ["Batman vs Iron Man"]
    finally:
        db.close()


@pytest.mark.parametrize("commit", HISTORICAL_COMMITS)
def test_historical_migration_is_idempotent(commit: str, tmp_path: Path) -> None:
    """Reopening a migrated historical database changes nothing further."""
    module = _load_historical_db_module(commit, tmp_path)
    path = tmp_path / f"court_{commit}.db"
    _seed_with_historical_code(module, path)

    CourtDB(path).close()
    first_tables = _table_names(path)
    first_rulings = _raw_table(path, "rulings")

    for _ in range(2):
        db = CourtDB(path)
        assert db.get_schema_version() == 2
        db.close()

    assert _table_names(path) == first_tables
    assert _raw_table(path, "rulings") == first_rulings
