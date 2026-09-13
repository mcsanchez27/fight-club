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
from bot.embeds import challenge_card_embed, verdict_embed
from bot.export import export_markdown
from bot.fights import (
    MISSING_FIGHT_PROMPT,
    accept_fight,
    apply_balance_to_fight,
    button_holder_id,
    counter_button_label,
    counter_fight,
    create_proposed_fight,
    decline_fight,
    expire_due_fights,
    fetch_and_store_accept_receipts,
    fill_open_ended_accept,
    format_opening_message,
    is_balance_free_counter_eligible,
    sides_complete,
    utc_now,
    validate_fight_fields,
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
        expire_due_fights(get_db(), utc_now())
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
        expire_due_fights(get_db(), utc_now())
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
        expire_due_fights(get_db(), utc_now())
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


class FightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rejudge_loop.start()

    def cog_unload(self) -> None:
        self.rejudge_loop.cancel()

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
        expire_due_fights(get_db(), utc_now())
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
