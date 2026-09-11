"""Challenge state is keyed by ruling message ID, not channel ID."""

from bot.commands import get_ruling, store_ruling, _last_verdict


def setup_function() -> None:
    _last_verdict.clear()


def test_store_and_get_by_message_id() -> None:
    v1 = {"matchup": "A vs B", "winner": "A"}
    v2 = {"matchup": "C vs D", "winner": "C"}
    store_ruling(1001, v1, fighter_a="A", fighter_b="B")
    store_ruling(1002, v2, fighter_a="C", fighter_b="D")
    assert get_ruling(1001)["verdict"] is v1
    assert get_ruling(1002)["verdict"] is v2
    assert get_ruling(1001)["verdict"] is not get_ruling(1002)["verdict"]


def test_channel_ids_do_not_collide_with_message_ids() -> None:
    older = {"matchup": "Old vs New", "winner": "Old"}
    newer = {"matchup": "Foo vs Bar", "winner": "Foo"}
    store_ruling(9001, older, fighter_a="Old", fighter_b="New")
    store_ruling(9002, newer, fighter_a="Foo", fighter_b="Bar")
    assert get_ruling(9001)["verdict"]["winner"] == "Old"
    assert get_ruling(9002)["verdict"]["winner"] == "Foo"


def test_fighters_and_context_stored_explicitly() -> None:
    v = {"matchup": "Team A vs Team B", "winner": "Team A"}
    store_ruling(
        55,
        v,
        fighter_a="Team A",
        fighter_b="Team B",
        context="no prep, random alley",
    )
    state = get_ruling(55)
    assert state is not None
    assert state["fighter_a"] == "Team A"
    assert state["fighter_b"] == "Team B"
    assert state["context"] == "no prep, random alley"
    assert state["verdict"] is v
