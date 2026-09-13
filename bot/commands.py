"""Slash commands and Challenge button view."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.budget import (
    assert_token_pack,
    check_budget,
    estimate_tokens,
    record_estimated_usage,
    usage_from_verdict,
)
from bot.db import get_db
from bot.embeds import challenge_card_embed, ruling_drop_embed, verdict_embed
from bot.records import (
    flare_for_winner,
    format_last5,
    format_streak_label,
    leaderboard as derive_leaderboard,
    record_for_user,
)
from bot.export import export_markdown
from bot.exhibits import contest_exhibits_on_message, prepare_judge_materials
from bot.fights import (
    JUDGE_READY_STATUS,
    MISSING_FIGHT_PROMPT,
    RULED_STATUS,
    accept_fight,
    actor_advocate_side,
    apply_balance_to_fight,
    archive_due,
    button_holder_id,
    cancel_fight,
    counter_button_label,
    counter_fight,
    create_proposed_fight,
    decline_fight,
    expire_due_fights,
    fetch_and_store_accept_receipts,
    fill_open_ended_accept,
    forfeit_fight,
    format_opening_message,
    is_balance_free_counter_eligible,
    judge_due_rests,
    rest_fight,
    sides_complete,
    sweep_deadlines,
    utc_now,
    validate_fight_fields,
)
from bot.ruling import (
    display_name,
    format_winner_one_liner,
    jump_url,
    reconsider_fight,
    rule_fight,
)
from bot.judge import judge
from bot.limits import limiter
from bot.progress import (
    PROGRESS_JUDGING,
    PROGRESS_RETRIEVING,
    RECEIPTS_GAP_NOTE,
    edit_deferred_progress,
)
from bot.retrieval import retrieve

log = logging.getLogger("fightclub")

_last_verdict: dict[int, dict[str, Any]] = {}


def store_ruling(
    message_id: int,
    verdict: dict[str, Any],
    *,
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    ruling_id: int | None = None,
) -> None:
    _last_verdict[message_id] = {
        "verdict": verdict,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "context": context,
        "ruling_id": ruling_id,
    }


def get_ruling(message_id: int) -> dict[str, Any] | None:
    state = _last_verdict.get(message_id)
    if state:
        return state
    row = get_db().get_ruling_by_message_id(message_id)
    if not row:
        return None
    state = {
        "verdict": row["verdict"],
        "fighter_a": row["fighter_a"],
        "fighter_b": row["fighter_b"],
        "context": row["context"],
        "ruling_id": row["id"],
    }
    _last_verdict[message_id] = state
    return state


def _persist_citations(ruling_id: int, verdict: dict[str, Any]) -> None:
    db = get_db()
    for c in verdict.get("citations") or []:
        if isinstance(c, str):
            db.insert_citation(
                ruling_id=ruling_id, claim=c, source_url=None, locator=None,
                snippet=None, verified=False, retrieved_at=None, kind="receipt",
            )
            continue
        if not isinstance(c, dict):
            continue
        db.insert_citation(
            ruling_id=ruling_id,
            claim=str(c.get("claim") or ""),
            source_url=str(c.get("source_url") or "") or None,
            locator=str(c.get("locator") or "") or None,
            snippet=str(c.get("snippet") or "") or None,
            verified=bool(c.get("verified")),
            retrieved_at=str(c.get("retrieved_at") or "") or None,
            kind=str(c.get("kind") or "receipt"),
        )


def _persist_ruling(
    *,
    message_id: int,
    channel_id: int | None,
    guild_id: int | None,
    fighter_a: str,
    fighter_b: str,
    context: str | None,
    verdict: dict[str, Any],
    parent_ruling_id: int | None = None,
) -> int:
    voided = bool(verdict.get("voided"))
    retrieval_status = verdict.get("retrieval_status")
    franchise = verdict.get("franchise")
    ruling_id = get_db().insert_ruling(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id,
        fighter_a=fighter_a,
        fighter_b=fighter_b,
        context=context,
        verdict=verdict,
        parent_ruling_id=parent_ruling_id,
        franchise=franchise if isinstance(franchise, str) else None,
        retrieval_status=retrieval_status if isinstance(retrieval_status, str) else None,
        voided=voided,
    )
    _persist_citations(ruling_id, verdict)
    # Only auto-retry hard fetch failures. Unlisted/disabled never become
    # ok without a config change, so don't spin the queue forever.
    if retrieval_status == "unavailable":
        get_db().enqueue_rejudge(
            ruling_id,
            reason=f"retrieval_status={retrieval_status}",
        )
    store_ruling(
        message_id, verdict, fighter_a=fighter_a, fighter_b=fighter_b,
        context=context, ruling_id=ruling_id,
    )
    u = usage_from_verdict(verdict)
    tin = u["tokens_in"]
    tout = u["tokens_out"]
    # Fallback to design-doc estimates when the provider did not return usage.
    if tin <= 0 and tout <= 0:
        tin = 5000 if retrieval_status == "ok" else 2500
        tout = 1000
    record_estimated_usage(
        tokens_in=tin,
        tokens_out=tout,
        retrieval_seconds=u["retrieval_seconds"],
        judge_seconds=u["judge_seconds"],
        total_seconds=u["total_seconds"],
    )
    return ruling_id


def _preflight(interaction: discord.Interaction) -> str | None:
    reject = limiter.check(interaction.user.id, interaction.guild_id)
    if reject:
        return reject
    return check_budget()


class ChallengeModal(discord.ui.Modal, title="Challenge the ruling"):
    evidence = discord.ui.TextInput(
        label="New evidence / challenge",
        style=discord.TextStyle.paragraph,
        placeholder="Cite canon, logistics, or a win-condition the court missed…",
        required=True,
        max_length=1500,
    )

    def __init__(self, prior: dict[str, Any], *, fighter_a: str, fighter_b: str, context: str | None, parent_ruling_id: int | None):
        super().__init__()
        self.prior = prior
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        self.context = context
        self.parent_ruling_id = parent_ruling_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reject = _preflight(interaction)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await edit_deferred_progress(interaction, PROGRESS_RETRIEVING)
        try:
            result = await asyncio.to_thread(
                retrieve,
                self.fighter_a,
                self.fighter_b,
                self.context,
                exhibits=[str(self.evidence)],
            )
            await edit_deferred_progress(interaction, PROGRESS_JUDGING)
            verdict = await asyncio.to_thread(
                judge,
                self.fighter_a,
                self.fighter_b,
                self.context,
                self.prior,
                str(self.evidence),
                exhibits=[str(self.evidence)],
                retrieval_result=result,
            )
        except Exception as e:
            await interaction.followup.send(f"Re-judge failed: {e}", ephemeral=True)
            return
        limiter.record(interaction.user.id, interaction.guild_id)
        msg = await interaction.followup.send(
            content="⚖ Court revises on challenge:",
            embed=verdict_embed(verdict),
            view=ChallengeView(),
        )
        _persist_ruling(
            message_id=msg.id, channel_id=interaction.channel_id, guild_id=interaction.guild_id,
            fighter_a=self.fighter_a, fighter_b=self.fighter_b, context=self.context,
            verdict=verdict, parent_ruling_id=self.parent_ruling_id,
        )


class ChallengeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Challenge", style=discord.ButtonStyle.danger, custom_id="fightclub:challenge")
    async def challenge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        message = interaction.message
        state = get_ruling(message.id) if message is not None else None
        if not state:
            await interaction.response.send_message(
                "No ruling attached to this message to challenge. Run `/fight` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ChallengeModal(
                state["verdict"], fighter_a=state["fighter_a"], fighter_b=state["fighter_b"],
                context=state.get("context"), parent_ruling_id=state.get("ruling_id"),
            )
        )



def _fight_id_from_custom_id(custom_id: str | None, prefix: str) -> int | None:
    if not custom_id or not custom_id.startswith(prefix):
        return None
    tail = custom_id[len(prefix) :]
    try:
        return int(tail)
    except ValueError:
        return None


class CounterModal(discord.ui.Modal, title="Counter challenge"):
    """Counter modal: optional matchup/context + swap-sides checkbox (Q1)."""

    matchup = discord.ui.Label(
        text="Matchup (optional)",
        component=discord.ui.TextInput(
            placeholder='A vs B — leave blank to keep',
            required=False,
            max_length=200,
        ),
    )
    context = discord.ui.Label(
        text="Context (optional)",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Overwrite context only if non-empty",
            required=False,
            max_length=1000,
        ),
    )
    swap = discord.ui.Label(
        text="Swap sides",
        description="Flip side_a↔side_b and advocate_a↔advocate_b",
        component=discord.ui.Checkbox(default=False),
    )

    def __init__(self, fight_id: int) -> None:
        super().__init__()
        self.fight_id = int(fight_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        matchup_val = (self.matchup.component.value or "").strip() or None  # type: ignore[union-attr]
        context_val = (self.context.component.value or "").strip() or None  # type: ignore[union-attr]
        swap_val = bool(self.swap.component.value)  # type: ignore[union-attr]
        db = get_db()
        free = is_balance_free_counter_eligible(
            db.get_fight(self.fight_id) or {}, db=db
        )
        try:
            fight = counter_fight(
                get_db(),
                self.fight_id,
                now=utc_now(),
                actor_id=interaction.user.id,
                matchup=matchup_val,
                context=context_val,
                swap_sides=swap_val,
                count_against_limit=not free,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        if fight["status"] == "voided":
            embed = challenge_card_embed(fight)
            embed.color = discord.Color.dark_grey()
            await interaction.response.edit_message(
                content="Counters exhausted — fight **voided**.",
                embed=embed,
                view=None,
            )
            return

        # Re-run balance when both sides known (warning chrome + free-counter label).
        if sides_complete(fight):
            try:
                apply_balance_to_fight(get_db(), int(fight["id"]))
                fight = get_db().get_fight(int(fight["id"])) or fight
            except Exception as e:
                log.info("balance after counter skipped: %s", e)

        view = ChallengeCardView(int(fight["id"]))
        if interaction.client:
            interaction.client.add_view(view)
        holder = button_holder_id(fight)
        note = (
            f"Countered — buttons now with <@{holder}>."
            if holder is not None
            else "Countered."
        )
        await interaction.response.edit_message(
            content=note,
            embed=challenge_card_embed(fight),
            view=view,
        )


class OpenEndedAcceptModal(discord.ui.Modal, title="Name your champion"):
    """Open-ended Accept: challengee fills side_b (+ optional context)."""

    champion = discord.ui.Label(
        text="Your champion",
        component=discord.ui.TextInput(
            placeholder="Fighter name",
            required=True,
            max_length=100,
        ),
    )
    context = discord.ui.Label(
        text="Context (optional)",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            placeholder="Optional constraints",
            required=False,
            max_length=1000,
        ),
    )

    def __init__(self, fight_id: int) -> None:
        super().__init__()
        self.fight_id = int(fight_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        champion = (self.champion.component.value or "").strip()  # type: ignore[union-attr]
        context_val = (self.context.component.value or "").strip() or None  # type: ignore[union-attr]
        try:
            fill_open_ended_accept(
                get_db(),
                self.fight_id,
                now=utc_now(),
                actor_id=interaction.user.id,
                champion=champion,
                context=context_val,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await _complete_accept(interaction, self.fight_id, actor_id=interaction.user.id)


class ChallengeCardView(discord.ui.View):
    """Persistent Accept / Decline / Counter card (4a–4c)."""

    def __init__(self, fight_id: int, *, counter_label: str | None = None) -> None:
        super().__init__(timeout=None)
        self.fight_id = int(fight_id)
        accept = discord.ui.Button(
            label="Accept",
            style=discord.ButtonStyle.success,
            custom_id=f"fightclub:accept:{self.fight_id}",
        )
        decline = discord.ui.Button(
            label="Decline",
            style=discord.ButtonStyle.secondary,
            custom_id=f"fightclub:decline:{self.fight_id}",
        )
        db = get_db()
        label = counter_label or counter_button_label(
            db.get_fight(self.fight_id), db=db
        )
        counter = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"fightclub:counter:{self.fight_id}",
        )
        accept.callback = self.on_accept  # type: ignore[method-assign]
        decline.callback = self.on_decline  # type: ignore[method-assign]
        counter.callback = self.on_counter  # type: ignore[method-assign]
        self.add_item(accept)
        self.add_item(decline)
        self.add_item(counter)

    async def on_accept(self, interaction: discord.Interaction) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight_id = self.fight_id
        fight = get_db().get_fight(fight_id)
        if fight is None:
            await interaction.response.send_message("Fight not found.", ephemeral=True)
            return
        # Open-ended with missing side_b → modal for champion (lead lock A1).
        if fight.get("open_ended") and not sides_complete(fight):
            holder = button_holder_id(fight)
            if holder is not None and int(interaction.user.id) != int(holder):
                await interaction.response.send_message(
                    "Only the challenged user can Accept.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(OpenEndedAcceptModal(fight_id))
            return
        await _complete_accept(interaction, fight_id, actor_id=interaction.user.id)

    async def on_decline(self, interaction: discord.Interaction) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight_id = self.fight_id
        try:
            fight = decline_fight(
                get_db(),
                fight_id,
                now=utc_now(),
                actor_id=interaction.user.id,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        embed = challenge_card_embed(fight)
        embed.color = discord.Color.dark_grey()
        await interaction.response.edit_message(
            content="Declined — fight **voided**.",
            embed=embed,
            view=None,
        )

    async def on_counter(self, interaction: discord.Interaction) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight = get_db().get_fight(self.fight_id)
        if fight is None:
            await interaction.response.send_message("Fight not found.", ephemeral=True)
            return
        holder = button_holder_id(fight)
        if holder is not None and int(interaction.user.id) != int(holder):
            await interaction.response.send_message(
                "Only the button holder can Counter.",
                ephemeral=True,
            )
            return
        if fight["status"] != "proposed":
            await interaction.response.send_message(
                f"Cannot counter a fight in status={fight['status']!r}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CounterModal(self.fight_id))


async def _complete_accept(
    interaction: discord.Interaction,
    fight_id: int,
    *,
    actor_id: int,
) -> None:
    """Accept path: progress → balance → receipts → thread/opening → arguing.

    Receipts are best-effort under the 10s budget (C2). Accept always succeeds
    even when retrieval is empty/unavailable. Never creates gallery exhibits.
    """
    db = get_db()
    fight = db.get_fight(fight_id)
    if fight is None:
        await interaction.response.send_message("Fight not found.", ephemeral=True)
        return

    # Progress edit (C2) — do not void if this fails.
    try:
        if interaction.response.is_done() is not True:
            await interaction.response.edit_message(
                content=PROGRESS_RETRIEVING,
                embed=challenge_card_embed(fight),
                view=None,
            )
        else:
            await edit_deferred_progress(interaction, PROGRESS_RETRIEVING)
    except Exception as e:
        log.info("accept progress skipped for fight %s: %s", fight_id, e)

    if sides_complete(fight):
        try:
            apply_balance_to_fight(db, fight_id)
        except Exception as e:
            log.info("balance before accept skipped: %s", e)

    # Receipts at accept (not at ruling) — never raises into a void.
    result = await asyncio.to_thread(fetch_and_store_accept_receipts, db, fight_id)

    thread_id = await _create_argument_thread(interaction, fight_id)

    try:
        fight = accept_fight(
            db,
            fight_id,
            now=utc_now(),
            actor_id=actor_id,
            thread_id=thread_id,
        )
    except ValueError as e:
        # Response may already be used by the progress edit.
        done = interaction.response.is_done()
        if done is True:
            await interaction.followup.send(str(e), ephemeral=True)
        else:
            await interaction.response.send_message(str(e), ephemeral=True)
        return

    gap = None
    receipts = getattr(result, "receipts", None) or []
    status = getattr(result, "status", None)
    if status != "ok" or not receipts:
        gap = RECEIPTS_GAP_NOTE
    await _respond_accepted(interaction, fight, receipt_note=gap)


async def _respond_accepted(
    interaction: discord.Interaction,
    fight: dict[str, Any],
    *,
    receipt_note: str | None = None,
) -> None:
    embed = challenge_card_embed(fight)
    embed.color = discord.Color.green()
    note = (
        f"Accepted — status **{fight['status']}**"
        + (
            f" · thread `{fight.get('thread_id')}`"
            if fight.get("thread_id")
            else " · thread unavailable"
        )
    )
    if receipt_note:
        note = f"{note}\n{receipt_note}"
    # Modal submits / progress edit may already have used the response.
    # Compare with ``is True`` so MagicMock in tests is not treated as done.
    done = interaction.response.is_done()
    if done is True:
        edit = getattr(interaction, "edit_original_response", None)
        if callable(edit):
            await edit(content=note, embed=embed, view=None)
        else:
            await interaction.followup.send(content=note, embed=embed)
    else:
        await interaction.response.edit_message(content=note, embed=embed, view=None)


async def _create_argument_thread(
    interaction: discord.Interaction, fight_id: int
) -> int | None:
    """Public thread under the challenge card + opening message (item 5)."""
    fight = get_db().get_fight(fight_id)
    message = interaction.message
    if fight is None or message is None:
        return None
    name = f"{fight.get('side_a') or 'A'} vs {fight.get('side_b') or 'B'}"
    name = name[:95] or f"fight-{fight_id}"
    create = getattr(message, "create_thread", None)
    if create is None:
        return None
    try:
        thread = await create(name=name, auto_archive_duration=1440)
    except Exception as e:
        log.info("thread create skipped for fight %s: %s", fight_id, e)
        return None

    opening = format_opening_message(fight)
    send = getattr(thread, "send", None)
    if callable(send):
        try:
            sent = send(opening)
            if asyncio.iscoroutine(sent) or asyncio.isfuture(sent):
                await sent  # type: ignore[misc]
        except Exception as e:
            log.info("opening message skipped for fight %s: %s", fight_id, e)

    tid = int(getattr(thread, "id", 0) or 0) or None
    return tid



def _channel_is_thread(channel: Any) -> bool:
    """True for Discord threads (and MagicMock threads that set ``type``)."""
    if channel is None:
        return False
    if isinstance(channel, discord.Thread):
        return True
    ctype = getattr(channel, "type", None)
    thread_types = {
        discord.ChannelType.public_thread,
        discord.ChannelType.private_thread,
    }
    news = getattr(discord.ChannelType, "news_thread", None)
    if news is not None:
        thread_types.add(news)
    return ctype in thread_types


def _fight_from_thread(interaction: discord.Interaction) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the in-thread fight, or return (None, ephemeral error)."""
    channel = interaction.channel
    if not _channel_is_thread(channel):
        return None, "Use this command inside the fight's argument thread."
    thread_id = getattr(channel, "id", None)
    if thread_id is None:
        return None, "Use this command inside the fight's argument thread."
    fight = get_db().get_fight_by_thread(int(thread_id))
    if fight is None:
        return None, "No fight is linked to this thread."
    return fight, None


class ForfeitConfirmView(discord.ui.View):
    """Ephemeral confirm button for ``/forfeit`` (not persistent)."""

    def __init__(self, fight_id: int, actor_id: int) -> None:
        super().__init__(timeout=120)
        self.fight_id = int(fight_id)
        self.actor_id = int(actor_id)

    @discord.ui.button(
        label="Confirm forfeit",
        style=discord.ButtonStyle.danger,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if int(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(
                "Only the advocate who started /forfeit can confirm.",
                ephemeral=True,
            )
            return
        sweep_deadlines(get_db(), utc_now())
        try:
            fight = forfeit_fight(
                get_db(),
                self.fight_id,
                now=utc_now(),
                actor_id=interaction.user.id,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(
            content=(
                f"Forfeited — fight **forfeited** (you take the L). "
                f"Status `{fight['status']}`."
            ),
            view=None,
        )



def _attachment_to_dict(att: Any) -> dict[str, Any]:
    return {
        "id": getattr(att, "id", None),
        "url": getattr(att, "url", None),
        "filename": getattr(att, "filename", None),
        "content_type": getattr(att, "content_type", None),
    }


def message_to_dict(msg: Any) -> dict[str, Any]:
    """Normalize a Discord message (or message-like) for transcript/exhibits."""
    if isinstance(msg, dict):
        return msg
    author = getattr(msg, "author", None)
    author_id = getattr(author, "id", None) if author is not None else None
    attachments = [
        _attachment_to_dict(a) for a in (getattr(msg, "attachments", None) or [])
    ]
    return {
        "id": getattr(msg, "id", None),
        "author_id": author_id,
        "content": getattr(msg, "content", None) or "",
        "attachments": attachments,
    }



async def _archive_fight_thread(fight: dict[str, Any], bot: Any | None = None) -> None:
    """Best-effort Discord thread archive for archive_due callback."""
    thread_id = fight.get("thread_id")
    if thread_id is None:
        return
    # Prefer bot.fetch_channel when available; otherwise no-op (tests mock archive_due).
    if bot is None:
        return
    try:
        channel = bot.get_channel(int(thread_id)) or await bot.fetch_channel(int(thread_id))
    except Exception as e:
        log.warning("archive fetch thread %s failed: %s", thread_id, e)
        return
    edit = getattr(channel, "edit", None)
    if not callable(edit):
        return
    try:
        await edit(archived=True)
    except Exception as e:
        log.warning("archive thread %s failed: %s", thread_id, e)
        raise


async def drop_ruling_messages(
    *,
    thread: Any,
    parent_channel: Any,
    result: dict[str, Any],
    winner_display: str,
) -> Any:
    """Post full embed in thread + channel one-liner; stamp ruling message_id."""
    fight = result["fight"]
    verdict = result["verdict"]
    flare = result.get("flare_line")
    if not flare:
        winner_adv = None
        ws = (verdict or {}).get("winner_side")
        if ws in {"a", "b"}:
            winner_adv = fight.get(f"advocate_{ws}_id")
        if winner_adv is not None and fight.get("guild_id") is not None:
            try:
                flare = flare_for_winner(
                    get_db(),
                    guild_id=int(fight["guild_id"]),
                    winner_advocate_id=int(winner_adv),
                    winner_name=winner_display,
                )
            except Exception as e:
                log.warning("flare compute failed: %s", e)
                flare = None
    embed = ruling_drop_embed(
        verdict,
        fight=fight,
        thin_record=bool(result.get("thin_record")),
        exhibit_ledger=result.get("exhibit_ledger"),
        flare_line=flare,
    )
    thread_msg = await thread.send(embed=embed)
    ruling_id = result.get("ruling_id")
    if ruling_id is not None:
        try:
            get_db().update_ruling(
                int(ruling_id),
                message_id=int(thread_msg.id),
                channel_id=int(getattr(thread, "id", None) or fight.get("thread_id") or 0) or None,
            )
        except Exception as e:
            log.warning("update ruling message_id failed: %s", e)
    link = jump_url(
        fight.get("guild_id"),
        getattr(thread, "id", None) or fight.get("thread_id"),
        getattr(thread_msg, "id", None),
    )
    one_liner = format_winner_one_liner(
        winner_name=winner_display,
        side_a=str(fight.get("side_a") or "?"),
        side_b=str(fight.get("side_b") or "?"),
        jump_link=link,
    )
    if parent_channel is not None and hasattr(parent_channel, "send"):
        try:
            await parent_channel.send(one_liner)
        except Exception as e:
            log.warning("channel one-liner failed: %s", e)
    return thread_msg


RECONSIDERATION_EMBED_TITLE = "Ruling on reconsideration"


async def drop_reconsideration_messages(
    *,
    thread: Any,
    result: dict[str, Any],
) -> Any:
    """Post the second embed titled "Ruling on reconsideration" with one-line diff."""
    fight = result["fight"]
    verdict = result["verdict"]
    flare = result.get("flare_line")
    if not flare:
        winner_adv = None
        ws = (verdict or {}).get("winner_side")
        if ws in {"a", "b"}:
            winner_adv = fight.get(f"advocate_{ws}_id")
        if winner_adv is not None and fight.get("guild_id") is not None:
            try:
                side_name = str(fight.get(f"side_{ws}") or ws.upper())
                flare = flare_for_winner(
                    get_db(),
                    guild_id=int(fight["guild_id"]),
                    winner_advocate_id=int(winner_adv),
                    winner_name=side_name,
                )
            except Exception as e:
                log.warning("flare compute failed (reconsider): %s", e)
                flare = None
    embed = ruling_drop_embed(
        verdict,
        fight=fight,
        thin_record=bool(result.get("thin_record")),
        exhibit_ledger=result.get("exhibit_ledger"),
        flare_line=flare,
        title=RECONSIDERATION_EMBED_TITLE,
        diff_line=result.get("diff_line") or (verdict or {}).get("reconsideration_diff"),
    )
    thread_msg = await thread.send(embed=embed)
    ruling_id = result.get("ruling_id")
    if ruling_id is not None:
        try:
            get_db().update_ruling(
                int(ruling_id),
                message_id=int(thread_msg.id),
                channel_id=int(
                    getattr(thread, "id", None) or fight.get("thread_id") or 0
                )
                or None,
            )
        except Exception as e:
            log.warning("update reconsideration message_id failed: %s", e)
    return thread_msg


async def fetch_thread_message_dicts(channel: Any, *, limit: int = 500) -> list[dict[str, Any]]:
    """Pull thread history newest-last → chronological message-like dicts."""
    if channel is None or not hasattr(channel, "history"):
        return []
    collected: list[Any] = []
    try:
        async for msg in channel.history(limit=limit, oldest_first=True):
            collected.append(msg)
    except Exception as e:
        log.warning("thread history fetch failed: %s", e)
        return []
    return [message_to_dict(m) for m in collected]


class FightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rejudge_loop.start()

    def cog_unload(self) -> None:
        self.rejudge_loop.cancel()

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Opposing advocate ❌ on an advocate message → contested exhibits."""
        # Spec: ❌ only (U+274C CROSS MARK).
        name = getattr(payload.emoji, "name", None) or str(payload.emoji)
        if str(payload.emoji) != "\u274c" and name != "\u274c" and str(payload.emoji) != "❌":
            if name != "❌":
                return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return
        db = get_db()
        fight = db.get_fight_by_thread(int(payload.channel_id))
        if fight is None:
            return
        if fight.get("status") not in {"arguing", "resting", JUDGE_READY_STATUS, "ruled"}:
            return
        try:
            contest_exhibits_on_message(
                db,
                fight,
                message_id=int(payload.message_id),
                reactor_id=int(payload.user_id),
            )
        except Exception as e:
            log.warning("contest reaction failed: %s", e)

    @tasks.loop(minutes=15)
    async def rejudge_loop(self) -> None:
        await self._process_rejudge_queue()

    @rejudge_loop.before_loop
    async def before_rejudge_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_rejudge_queue(self) -> None:
        db = get_db()
        pending = db.list_pending_rejudges(limit=5)
        for item in pending:
            if check_budget(db):
                break
            fa, fb = item["fighter_a"], item["fighter_b"]
            ctx = item.get("context")
            franchise = item.get("franchise")
            try:
                result = await asyncio.to_thread(retrieve, fa, fb, ctx, franchise_hint=franchise)
            except Exception as e:
                log.warning("rejudge retrieve failed for %s: %s", item["ruling_id"], e)
                continue
            if result.retrieval_unavailable:
                continue
            try:
                verdict = await asyncio.to_thread(
                    judge, fa, fb, ctx, item.get("verdict"),
                    "Automatic re-judge: retrieval restored.",
                    franchise=franchise, retrieval_result=result,
                )
            except Exception as e:
                log.warning("rejudge judge failed for %s: %s", item["ruling_id"], e)
                db.mark_rejudge_done(item["ruling_id"], status="failed")
                continue
            channel_id = item.get("channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            message_id = None
            if channel is not None and hasattr(channel, "send"):
                try:
                    msg = await channel.send(
                        content=f"⚖ Automatic re-judge for voided ruling #{item['ruling_id']} (retrieval restored):",
                        embed=verdict_embed(verdict), view=ChallengeView(),
                    )
                    message_id = msg.id
                except Exception as e:
                    log.warning("rejudge send failed: %s", e)
            new_id = _persist_ruling(
                message_id=message_id or 0, channel_id=channel_id, guild_id=item.get("guild_id"),
                fighter_a=fa, fighter_b=fb, context=ctx, verdict=verdict,
                parent_ruling_id=item["ruling_id"],
            )
            db.mark_rejudge_done(item["ruling_id"], status="done")
            log.info("rejudge complete parent=%s new=%s status=%s", item["ruling_id"], new_id, verdict.get("retrieval_status"))

    @app_commands.command(name="fight", description="Challenge a user or judge an instant matchup")
    @app_commands.describe(
        opponent="Who you are challenging (proposed card path)",
        matchup='Matchup label, e.g. "Aragorn vs Goku"',
        context="Optional arena, rules, or constraints",
        side="Your side: A, B, or a fighter name from the matchup",
        instant="If true, skip the card and rule immediately (V1 bridge)",
        fighter_a="V1/instant: first fighter (optional bridge until item 12)",
        fighter_b="V1/instant: second fighter (optional bridge until item 12)",
        franchise="Optional franchise key/label (instant path)",
        exhibits="Optional user-pasted evidence (instant path)",
    )
    async def fight(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member | None = None,
        matchup: str | None = None,
        context: str | None = None,
        side: str | None = None,
        instant: bool = False,
        fighter_a: str | None = None,
        fighter_b: str | None = None,
        franchise: str | None = None,
        exhibits: str | None = None,
    ) -> None:
        sweep_deadlines(get_db(), utc_now())
        plan = validate_fight_fields(
            opponent_id=opponent.id if opponent is not None else None,
            matchup=matchup,
            context=context,
            side=side,
            instant=instant,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
        )
        if plan.kind == "prompt":
            await interaction.response.send_message(
                plan.prompt or MISSING_FIGHT_PROMPT,
                ephemeral=True,
            )
            return

        if plan.kind == "proposed":
            assert opponent is not None and plan.side_a
            if not plan.open_ended:
                assert plan.side_b
            if opponent.id == interaction.user.id:
                await interaction.response.send_message(
                    "You cannot challenge yourself.",
                    ephemeral=True,
                )
                return
            fight = create_proposed_fight(
                get_db(),
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                challenger_id=interaction.user.id,
                challengee_id=opponent.id,
                side_a=plan.side_a,
                side_b=plan.side_b,
                context=plan.context,
                now=utc_now(),
                open_ended=plan.open_ended,
            )
            # Balance only when both sides known (skip open-ended until Accept modal).
            if sides_complete(fight):
                try:
                    apply_balance_to_fight(get_db(), int(fight["id"]))
                    fight = get_db().get_fight(int(fight["id"])) or fight
                except Exception as e:
                    log.info("balance at propose skipped: %s", e)
            view = ChallengeCardView(int(fight["id"]))
            self.bot.add_view(view)
            content = (
                f"{opponent.mention} — open-ended challenge (name your champion on Accept)."
                if plan.open_ended
                else f"{opponent.mention} — you've been challenged."
            )
            await interaction.response.send_message(
                content=content,
                embed=challenge_card_embed(fight),
                view=view,
            )
            sent = await interaction.original_response()
            get_db().update_fight(int(fight["id"]), card_message_id=int(sent.id))
            return

        # Instant bridge (item 12 will park this on fight rows).
        assert plan.side_a and plan.side_b
        fa, fb = plan.side_a, plan.side_b
        reject = _preflight(interaction)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        exhibit_list = [exhibits] if exhibits and exhibits.strip() else []
        await edit_deferred_progress(interaction, PROGRESS_RETRIEVING)
        try:
            result = await asyncio.to_thread(
                retrieve, fa, fb, plan.context,
                franchise_hint=franchise, exhibits=exhibit_list,
            )
            from bot.retrieval import pack_retrieval_for_prompt
            approx_prompt = pack_retrieval_for_prompt(result) + ("x" * 4000)
            tok_reject = assert_token_pack(estimate_tokens(approx_prompt), max_out=2048)
            if tok_reject:
                await interaction.followup.send(tok_reject, ephemeral=True)
                return
            await edit_deferred_progress(interaction, PROGRESS_JUDGING)
            verdict = await asyncio.to_thread(
                judge, fa, fb, plan.context, None, None,
                exhibits=exhibit_list, franchise=franchise, retrieval_result=result,
            )
        except Exception as e:
            await interaction.followup.send(f"Judgment failed: {e}", ephemeral=True)
            return
        limiter.record(interaction.user.id, interaction.guild_id)
        msg = await interaction.followup.send(embed=verdict_embed(verdict), view=ChallengeView())
        _persist_ruling(
            message_id=msg.id, channel_id=interaction.channel_id, guild_id=interaction.guild_id,
            fighter_a=fa, fighter_b=fb, context=plan.context, verdict=verdict,
            parent_ruling_id=None,
        )

    @app_commands.command(name="rest", description="Rest your case (advocates, fight thread only)")
    async def rest_cmd(self, interaction: discord.Interaction) -> None:
        def _archive_cb(f: dict[str, Any]) -> None:
            # Schedule is sync from sweep; archive best-effort via create_task when loop runs.
            try:
                asyncio.get_running_loop().create_task(
                    _archive_fight_thread(f, self.bot)
                )
            except RuntimeError:
                pass

        sweep_deadlines(get_db(), utc_now(), archive_thread=_archive_cb)
        fight, err = _fight_from_thread(interaction)
        if err or fight is None:
            await interaction.response.send_message(err or "Fight not found.", ephemeral=True)
            return
        try:
            out = rest_fight(
                get_db(),
                int(fight["id"]),
                now=utc_now(),
                actor_id=interaction.user.id,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if out["status"] == JUDGE_READY_STATUS or out["status"] == RULED_STATUS:
            # Item 8: ensure snapshot (one live fetch if null), then rule + drop.
            if out["status"] == RULED_STATUS:
                await interaction.response.send_message(
                    "This fight is already **ruled**.", ephemeral=True
                )
                return
            if not interaction.response.is_done():
                await interaction.response.defer(thinking=True)
            msgs = None
            if out.get("transcript_snapshot") is None:
                msgs = await fetch_thread_message_dicts(interaction.channel)
            await edit_deferred_progress(interaction, PROGRESS_JUDGING)
            try:
                result = await asyncio.to_thread(
                    rule_fight,
                    get_db(),
                    int(out["id"]),
                    now=utc_now(),
                    messages=msgs,
                )
            except Exception as e:
                await interaction.followup.send(f"Ruling failed: {e}", ephemeral=True)
                return
            if result.get("already_ruled"):
                await interaction.followup.send("Already ruled — no double call.")
                return
            fight_row = result["fight"]
            winner_side = (result.get("verdict") or {}).get("winner_side")
            winner_name = "?"
            if winner_side in {"a", "b"}:
                adv_id = fight_row.get(f"advocate_{winner_side}_id")
                # Prefer interaction guild member display name when available.
                member = None
                if adv_id is not None and interaction.guild is not None:
                    member = interaction.guild.get_member(int(adv_id))
                if member is not None:
                    winner_name = display_name(member)
                else:
                    side_label = str(fight_row.get(f"side_{winner_side}") or winner_side.upper())
                    winner_name = side_label
            parent = getattr(interaction.channel, "parent", None)
            try:
                await drop_ruling_messages(
                    thread=interaction.channel,
                    parent_channel=parent,
                    result=result,
                    winner_display=winner_name,
                )
            except Exception as e:
                log.warning("ruling drop failed: %s", e)
                await interaction.followup.send(
                    f"Ruled, but drop failed: {e}", ephemeral=True
                )
                return
            note = "Ruling posted."
            if result.get("thin_record"):
                note += " Thin record banner applied."
            await interaction.followup.send(note)
            return
        elif out.get("rest_a_at") and not out.get("rest_b_at"):
            msg = (
                "Side A rested — status **resting**. "
                f"Other side has until `{out.get('rest_deadline_at')}` "
                "(or `/rest`) before judge-ready."
            )
        elif out.get("rest_b_at") and not out.get("rest_a_at"):
            msg = (
                "Side B rested — status **resting**. "
                f"Other side has until `{out.get('rest_deadline_at')}` "
                "(or `/rest`) before judge-ready."
            )
        else:
            msg = f"Rested — status **{out['status']}**."
        await interaction.response.send_message(msg)

    @app_commands.command(name="forfeit", description="Forfeit the fight (advocates, fight thread only)")
    async def forfeit_cmd(self, interaction: discord.Interaction) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight, err = _fight_from_thread(interaction)
        if err or fight is None:
            await interaction.response.send_message(err or "Fight not found.", ephemeral=True)
            return
        if actor_advocate_side(fight, interaction.user.id) is None:
            await interaction.response.send_message(
                "Only advocates can /forfeit.", ephemeral=True
            )
            return
        if fight["status"] not in {"arguing", "resting"}:
            await interaction.response.send_message(
                f"Cannot forfeit a fight in status={fight['status']!r}.",
                ephemeral=True,
            )
            return
        view = ForfeitConfirmView(int(fight["id"]), interaction.user.id)
        await interaction.response.send_message(
            "Confirm forfeit? You take the L. This cannot be undone.",
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="cancel", description="Request mutual cancel (advocates, fight thread only)")
    async def cancel_cmd(self, interaction: discord.Interaction) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight, err = _fight_from_thread(interaction)
        if err or fight is None:
            await interaction.response.send_message(err or "Fight not found.", ephemeral=True)
            return
        try:
            out = cancel_fight(
                get_db(),
                int(fight["id"]),
                now=utc_now(),
                actor_id=interaction.user.id,
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if out["status"] == "voided":
            msg = "Both advocates agreed — fight **voided**."
        else:
            msg = (
                "Cancel requested. Waiting for the other advocate to `/cancel`."
            )
        await interaction.response.send_message(msg)

    @app_commands.command(
        name="reconsider",
        description="Motion for reconsideration (advocates, once per fight, evidence required)",
    )
    @app_commands.describe(evidence="New evidence the court missed (required, non-empty)")
    async def reconsider_cmd(
        self, interaction: discord.Interaction, evidence: str
    ) -> None:
        sweep_deadlines(get_db(), utc_now())
        fight, err = _fight_from_thread(interaction)
        if err or fight is None:
            await interaction.response.send_message(
                err or "Fight not found.", ephemeral=True
            )
            return
        if actor_advocate_side(fight, interaction.user.id) is None:
            await interaction.response.send_message(
                "Only advocates can /reconsider.", ephemeral=True
            )
            return
        evidence_s = (evidence or "").strip()
        if not evidence_s:
            await interaction.response.send_message(
                "Evidence is required for /reconsider (non-empty string).",
                ephemeral=True,
            )
            return
        if fight.get("status") != RULED_STATUS:
            await interaction.response.send_message(
                f"Can only /reconsider a **ruled** fight (status={fight.get('status')!r}).",
                ephemeral=True,
            )
            return
        reject = _preflight(interaction)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await edit_deferred_progress(interaction, PROGRESS_JUDGING)
        try:
            result = await asyncio.to_thread(
                reconsider_fight,
                get_db(),
                int(fight["id"]),
                evidence=evidence_s,
                now=utc_now(),
                actor_id=interaction.user.id,
            )
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(
                f"Reconsideration failed: {e}", ephemeral=True
            )
            return
        limiter.record(interaction.user.id, interaction.guild_id)
        try:
            await drop_reconsideration_messages(
                thread=interaction.channel,
                result=result,
            )
        except Exception as e:
            log.warning("reconsideration drop failed: %s", e)
            await interaction.followup.send(
                f"Reconsidered, but drop failed: {e}", ephemeral=True
            )
            return
        diff = result.get("diff_line") or ""
        await interaction.followup.send(
            f"Reconsideration posted. {diff}".strip()
        )

    @app_commands.command(name="export", description="Export a ruling as a markdown block for paste-anywhere")
    @app_commands.describe(message_id="Discord message ID of the ruling (default: reply/reference or latest you can see)")
    async def export_cmd(self, interaction: discord.Interaction, message_id: str | None = None) -> None:
        mid: int | None = None
        if message_id:
            try:
                mid = int(message_id.strip())
            except ValueError:
                await interaction.response.send_message("message_id must be an integer snowflake.", ephemeral=True)
                return
        elif interaction.message and interaction.message.reference:
            mid = interaction.message.reference.message_id
        row = None
        state = None
        if mid is not None:
            state = get_ruling(mid)
            row = get_db().get_ruling_by_message_id(mid)
        elif interaction.guild_id is not None:
            rows = get_db().list_guild_rulings(interaction.guild_id, limit=1)
            row = rows[0] if rows else None
        if not row and not state:
            await interaction.response.send_message("No ruling found. Pass message_id or run `/fight` first.", ephemeral=True)
            return
        if row:
            verdict = row["verdict"]
            fa, fb, ctx = row["fighter_a"], row["fighter_b"], row.get("context")
            franchise = row.get("franchise")
            status = row.get("retrieval_status")
            voided = bool(row.get("voided"))
            cites = get_db().list_citations(row["id"]) or verdict.get("citations")
        else:
            assert state is not None
            verdict = state["verdict"]
            fa, fb, ctx = state["fighter_a"], state["fighter_b"], state.get("context")
            franchise = verdict.get("franchise")
            status = verdict.get("retrieval_status")
            voided = bool(verdict.get("voided"))
            cites = verdict.get("citations")
        md = export_markdown(verdict, fighter_a=fa, fighter_b=fb, context=ctx, franchise=franchise, retrieval_status=status, voided=voided, citations=cites)
        if len(md) > 1900:
            md = md[:1890] + "\n```"
        await interaction.response.send_message(md, ephemeral=True)

    @app_commands.command(name="standings", description="Recent Fight Club rulings in this server")
    @app_commands.describe(limit="How many recent rulings to show (1-25, default 10)")
    async def standings(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Standings are only available in a server.", ephemeral=True)
            return
        rows = get_db().list_guild_rulings(interaction.guild_id, limit=int(limit))
        if not rows:
            await interaction.response.send_message("No rulings on the record yet. Run `/fight`.", ephemeral=True)
            return
        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            v = row["verdict"]
            matchup = v.get("matchup") or f"{row['fighter_a']} vs {row['fighter_b']}"
            winner = v.get("winner", "?")
            conf = v.get("confidence", "?")
            revised = "revised" if row.get("parent_ruling_id") else "original"
            void = " · voided" if row.get("voided") else ""
            lines.append(f"{i}. **{matchup}** — {winner} ({conf}/10) · {revised}{void}")
        embed = discord.Embed(title="⚔ Court standings", description="\n".join(lines), color=discord.Color.dark_gold())
        embed.set_footer(text=f"Last {len(rows)} ruling(s) in this server")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Server Fight Club leaderboard (top 15)")
    async def leaderboard_cmd(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Leaderboard is only available in a server.", ephemeral=True
            )
            return
        rows = derive_leaderboard(get_db(), int(interaction.guild_id), limit=15)
        if not rows:
            await interaction.response.send_message(
                "No countable records yet. Finish a fight or forfeit.",
                ephemeral=True,
            )
            return
        lines: list[str] = []
        guild = interaction.guild
        for i, rec in enumerate(rows, start=1):
            name = f"User {rec.user_id}"
            if guild is not None:
                member = guild.get_member(int(rec.user_id))
                if member is not None:
                    name = display_name(member)
            streak = format_streak_label(rec.streak, rec.streak_kind)
            pct = f"{rec.win_pct * 100:.0f}%"
            lines.append(
                f"{i}. **{name}** — {rec.wins}-{rec.losses} ({pct}) · streak {streak}"
            )
        embed = discord.Embed(
            title="🏆 Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Derived W/L · voided/expired/instant excluded")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="record", description="Show a member's Fight Club record")
    @app_commands.describe(user="Member to look up (defaults to you)")
    async def record_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Records are only available in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        uid = int(target.id)
        rec = record_for_user(get_db(), int(interaction.guild_id), uid)
        name = display_name(target)
        streak = format_streak_label(rec.streak, rec.streak_kind)
        last5 = format_last5(rec.last5)
        total = rec.wins + rec.losses
        if total == 0:
            body = f"**{name}** has no countable fights yet."
        else:
            body = (
                f"**{name}** — **{rec.wins}-{rec.losses}**\n"
                f"Current streak: **{streak}**\n"
                f"Last 5: `{last5}`"
            )
        embed = discord.Embed(
            title="📜 Record",
            description=body,
            color=discord.Color.dark_gold(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="laws", description="List the Laws of the Court")
    async def laws(self, interaction: discord.Interaction) -> None:
        from bot.laws import get_laws
        text = get_laws().strip()
        if not text:
            await interaction.response.send_message("No laws loaded. Check laws.md on the bot host.", ephemeral=True)
            return
        body = text if len(text) <= 4000 else text[:3999] + "…"
        embed = discord.Embed(title="⚖ Laws of the Court", description=body, color=discord.Color.dark_teal())
        await interaction.response.send_message(embed=embed)

    docket = app_commands.Group(name="docket", description="Banked matchups for this server")

    @docket.command(name="add", description="Bank a matchup for later")
    @app_commands.describe(matchup='Matchup label, e.g. "Goku vs Superman"', notes="Optional notes / constraints")
    async def docket_add(self, interaction: discord.Interaction, matchup: str, notes: str | None = None) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Docket is only available in a server.", ephemeral=True)
            return
        rid = get_db().add_docket(interaction.guild_id, matchup.strip(), notes)
        await interaction.response.send_message(
            f"Docketed #{rid}: **{matchup.strip()}**" + (f" — _{notes}_" if notes else ""),
            ephemeral=True,
        )

    @docket.command(name="list", description="List banked matchups for this server")
    @app_commands.describe(limit="How many entries to show (1-25, default 15)")
    async def docket_list(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 15) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Docket is only available in a server.", ephemeral=True)
            return
        rows = get_db().list_docket(interaction.guild_id, limit=int(limit))
        if not rows:
            await interaction.response.send_message("Docket is empty. Use `/docket add`.", ephemeral=True)
            return
        lines = []
        for row in rows:
            note = f" — _{row['notes']}_" if row.get("notes") else ""
            lines.append(f"#{row['id']}: **{row['matchup']}**{note}")
        embed = discord.Embed(title="📋 Banked docket", description="\n".join(lines), color=discord.Color.dark_blue())
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    from bot.laws import load_laws
    db = get_db()
    load_laws()
    await bot.add_cog(FightCog(bot))
    bot.add_view(ChallengeView())
    # Re-bind persistent Accept/Decline views for open proposed fights.
    for fight in db.list_fights(status="proposed"):
        bot.add_view(ChallengeCardView(int(fight["id"])))
