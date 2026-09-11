"""SQLite court.db round-trip."""

from pathlib import Path

from bot.db import CourtDB


def _verdict(**overrides):
    v = {
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
    v.update(overrides)
    return v


def test_insert_and_fetch_by_message_id(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    rid = db.insert_ruling(
        message_id=111,
        channel_id=222,
        guild_id=333,
        fighter_a="Goku",
        fighter_b="Superman",
        context="no prep",
        verdict=_verdict(),
        parent_ruling_id=None,
    )
    assert rid >= 1
    row = db.get_ruling_by_message_id(111)
    assert row is not None
    assert row["id"] == rid
    assert row["fighter_a"] == "Goku"
    assert row["fighter_b"] == "Superman"
    assert row["context"] == "no prep"
    assert row["verdict"]["winner"] == "A"
    assert row["parent_ruling_id"] is None
    db.close()


def test_challenge_creates_new_row_linked_to_parent(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    parent = db.insert_ruling(
        message_id=1,
        channel_id=2,
        guild_id=3,
        fighter_a="A",
        fighter_b="B",
        context=None,
        verdict=_verdict(winner="A"),
    )
    child = db.insert_ruling(
        message_id=10,
        channel_id=2,
        guild_id=3,
        fighter_a="A",
        fighter_b="B",
        context=None,
        verdict=_verdict(winner="B", ruling="revised"),
        parent_ruling_id=parent,
    )
    assert child != parent
    row = db.get_ruling_by_id(child)
    assert row["parent_ruling_id"] == parent
    # parent unchanged
    original = db.get_ruling_by_id(parent)
    assert original["verdict"]["winner"] == "A"
    db.close()


def test_list_guild_rulings(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    for i in range(3):
        db.insert_ruling(
            message_id=100 + i,
            channel_id=1,
            guild_id=7,
            fighter_a=f"A{i}",
            fighter_b=f"B{i}",
            context=None,
            verdict=_verdict(matchup=f"A{i} vs B{i}", winner=f"A{i}"),
        )
    rows = db.list_guild_rulings(7, limit=2)
    assert len(rows) == 2
    db.close()


def test_docket_add_and_list(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    i1 = db.add_docket(5, "A vs B", notes="open field")
    i2 = db.add_docket(5, "C vs D")
    assert i1 != i2
    rows = db.list_docket(5, limit=10)
    assert len(rows) == 2
    assert rows[0]["matchup"] in {"A vs B", "C vs D"}
    assert db.list_docket(99) == []
    db.close()
