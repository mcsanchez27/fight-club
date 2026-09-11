"""Challenge state is keyed by ruling message ID, not channel ID."""

from bot.commands import get_ruling, store_ruling, _last_verdict


def setup_function() -> None:
    _last_verdict.clear()


def test_store_and_get_by_message_id() -> None:
    v1 = {"matchup": "A vs B", "winner": "A"}
    v2 = {"matchup": "C vs D", "winner": "C"}
    store_ruling(1001, v1)
    store_ruling(1002, v2)
    assert get_ruling(1001) is v1
    assert get_ruling(1002) is v2
    assert get_ruling(1001) is not get_ruling(1002)


def test_channel_ids_do_not_collide_with_message_ids() -> None:
    """Two rulings in the same channel keep separate state by message id."""
    channel_id = 42
    older = {"matchup": "Old vs New", "winner": "Old"}
    newer = {"matchup": "Foo vs Bar", "winner": "Foo"}
    store_ruling(channel_id, older)  # would be wrong if keyed by channel
    store_ruling(9001, older)
    store_ruling(9002, newer)
    assert get_ruling(9001) == older
    assert get_ruling(9002) == newer
    # Challenging the older message must not return the newer ruling
    assert get_ruling(9001)["winner"] == "Old"
