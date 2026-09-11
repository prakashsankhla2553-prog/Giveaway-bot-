import asyncio
import logging
import os
import random
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "g.w") or "g.w"
try:
    TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or "0")
except ValueError:
    TEST_GUILD_ID = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("sab_giveaways")

intents = discord.Intents.none()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.invites = True


giveaways = {}
invite_counts = defaultdict(lambda: defaultdict(int))
invite_cache = {}
templates = defaultdict(dict)
rewards = {}
guild_settings = defaultdict(dict)
claim_tasks = {}
next_reward_id = 1


def utcnow():
    return datetime.now(timezone.utc)


def parse_duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(s|m|h|d|w)", value.strip().lower())
    if not match:
        raise ValueError("Use a duration such as 30m, 1h, 2d, or 1w.")
    amount = float(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    seconds = int(amount * multiplier)
    if seconds < 1 or seconds > 365 * 86400:
        raise ValueError("Duration must be between 1 second and 365 days.")
    return seconds


def format_duration(seconds):
    seconds = int(seconds)
    for suffix, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{suffix}"
    return f"{seconds}s"


def format_time(value):
    return discord.utils.format_dt(value, "R")


def mention_ids(ids):
    return ", ".join(f"<@{user_id}>" for user_id in ids) if ids else "None"


def setting(guild_id, key, default=None):
    return guild_settings[guild_id].get(key, default)


def requirement_text(giveaway):
    requirements = giveaway["requirements"]
    parts = []
    if requirements.get("invites") is not None:
        parts.append(f"{requirements['invites']} valid invites")
    if requirements.get("role_id"):
        parts.append(f"<@&{requirements['role_id']}>")
    if requirements.get("booster"):
        parts.append("Server Booster")
    if requirements.get("bypass_role_id"):
        parts.append(f"Bypass: <@&{requirements['bypass_role_id']}>")
    return "\n".join(parts) if parts else "None"


def extra_entry_text(guild):
    names = {role.name.casefold() for role in guild.roles}
    supported = []
    for name in ("2x Luck", "4x Luck", "8x Luck", "16x Luck", "Regular", "Active", "Most Active"):
        if name.casefold() in names:
            supported.append(name)
    return ", ".join(supported) if supported else "None configured"


def giveaway_embed(giveaway):
    ended = giveaway["status"] != "active"
    embed = discord.Embed(
        title="🎉 Giveaway Ended" if ended else "🎉 Giveaway",
        description=f"**{giveaway['prize']}**",
        color=discord.Color.from_rgb(255, 255, 255),
    )
    embed.add_field(name="🎁 Prize", value=giveaway["prize"], inline=False)
    embed.add_field(name="🏆 Winners", value=str(giveaway["winner_count"]), inline=True)
    embed.add_field(name="⏰ Ends", value=format_time(giveaway["ends_at"]), inline=True)
    role_id = giveaway.get("winner_role_id")
    embed.add_field(name="🏅 Winner Role", value=f"<@&{role_id}>" if role_id else "Not configured", inline=True)
    embed.add_field(name="👥 Participants", value=str(len(giveaway["participants"])), inline=True)
    embed.add_field(name="🍀 Extra Entries", value=extra_entry_text(giveaway["guild"]), inline=True)
    embed.add_field(name="🔒 Requirements", value=requirement_text(giveaway), inline=False)
    if ended:
        embed.add_field(name="🎉 Winner(s)", value=mention_ids(giveaway.get("winners", [])), inline=False)
        embed.set_footer(text=f"Status: {giveaway['status'].title()}")
    else:
        embed.set_footer(text=f"Giveaway ID: {giveaway['key']}")
    return embed


def reward_embed(reward):
    embed = discord.Embed(title="🎁 INVITE REWARD", color=discord.Color.gold())
    embed.add_field(name="🎁 Reward", value=reward["prize"], inline=False)
    embed.add_field(name="👥 Required Invites", value=str(reward["required"]), inline=True)
    if reward.get("role_id"):
        embed.add_field(name="🏅 Reward Role", value=f"<@&{reward['role_id']}", inline=True)
    embed.set_footer(text=f"Reward ID: {reward['id']}")
    return embed


def get_member(guild, user_id):
    return guild.get_member(user_id)


def highest_multiplier(member):
    multiplier = 1
    for role in member.roles:
        name = role.name.casefold().strip()
        match = re.fullmatch(r"(2|4|8|16)x\s*luck", name)
        if match:
            multiplier = max(multiplier, int(match.group(1)))
        elif name == "active":
            multiplier = max(multiplier, 2)
        elif name == "most active":
            multiplier = max(multiplier, 4)
        elif name == "regular":
            multiplier = max(multiplier, 1)
    return multiplier


def weighted_choice(guild, candidate_ids):
    members = [guild.get_member(user_id) for user_id in candidate_ids]
    pairs = [(member.id, highest_multiplier(member)) for member in members if member and not member.bot]
    if not pairs:
        return None
    return random.choices([pair[0] for pair in pairs], weights=[pair[1] for pair in pairs], k=1)[0]


def choose_winners(guild, giveaway, amount, excluded=None):
    excluded = set(excluded or ())
    candidates = [user_id for user_id in giveaway["participants"] if user_id not in excluded and guild.get_member(user_id)]
    selected = []
    while candidates and len(selected) < amount:
        winner = weighted_choice(guild, candidates)
        if winner is None:
            break
        selected.append(winner)
        candidates.remove(winner)
    return selected


async def fetch_giveaway_message(giveaway):
    channel = bot.get_channel(giveaway["channel_id"])
    if channel is None:
        channel = await bot.fetch_channel(giveaway["channel_id"])
    return channel, await channel.fetch_message(giveaway["message_id"])


async def edit_giveaway_message(giveaway, disabled=False):
    try:
        _, message = await fetch_giveaway_message(giveaway)
        await message.edit(embed=giveaway_embed(giveaway), view=GiveawayView(giveaway["key"], disabled=disabled))
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        logger.warning("Could not update giveaway message %s", giveaway.get("message_id"))


def winner_role(guild, giveaway=None):
    role_id = giveaway.get("winner_role_id") if giveaway else None
    role_id = role_id or setting(guild.id, "winner_role_id")
    role = guild.get_role(role_id) if role_id else None
    return role or discord.utils.find(lambda item: item.name.casefold() == "giveaway winner", guild.roles)


async def add_winner_role(guild, user_id, giveaway):
    role = winner_role(guild, giveaway)
    member = guild.get_member(user_id)
    if not role or not member:
        return False
    try:
        await member.add_roles(role, reason="Giveaway winner")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def remove_winner_role(guild, user_id, giveaway):
    role = winner_role(guild, giveaway)
    member = guild.get_member(user_id)
    if not role or not member:
        return
    try:
        await member.remove_roles(role, reason="Giveaway claim expired")
    except (discord.Forbidden, discord.HTTPException):
        pass


def cancel_claim_task(giveaway_key, user_id):
    task = claim_tasks.pop((giveaway_key, user_id), None)
    if task and not task.done():
        task.cancel()


async def claim_expiration(giveaway_key, user_id):
    giveaway = giveaways.get(giveaway_key)
    if not giveaway:
        return
    try:
        await asyncio.sleep(giveaway["claim_minutes"] * 60)
        giveaway = giveaways.get(giveaway_key)
        if not giveaway or giveaway["claims"].get(user_id) != "pending":
            return
        guild = bot.get_guild(giveaway["guild_id"])
        if guild is None:
            return
        giveaway["claims"][user_id] = "expired"
        await remove_winner_role(guild, user_id, giveaway)
        await replace_winner(giveaway, user_id, "claim period expired")
    except asyncio.CancelledError:
        return
    finally:
        claim_tasks.pop((giveaway_key, user_id), None)


async def replace_winner(giveaway, old_user_id, reason):
    guild = bot.get_guild(giveaway["guild_id"])
    if guild is None or giveaway["status"] != "ended":
        return
    giveaway["history"].add(old_user_id)
    excluded = set(giveaway["winners"]) | giveaway["history"]
    new_winners = choose_winners(guild, giveaway, 1, excluded)
    if not new_winners:
        new_winners = choose_winners(guild, giveaway, 1, set(giveaway["winners"]))
    if not new_winners:
        channel = bot.get_channel(giveaway["channel_id"])
        if channel:
            await channel.send(f"⚠️ No eligible replacement winner was available for **{giveaway['prize']}**.")
        return
    new_user_id = new_winners[0]
    try:
        index = giveaway["winners"].index(old_user_id)
        giveaway["winners"][index] = new_user_id
    except ValueError:
        giveaway["winners"].append(new_user_id)
    giveaway["claims"].pop(old_user_id, None)
    giveaway["claims"][new_user_id] = "pending"
    await add_winner_role(guild, new_user_id, giveaway)
    claim_tasks[(giveaway["key"], new_user_id)] = asyncio.create_task(claim_expiration(giveaway["key"], new_user_id))
    channel = bot.get_channel(giveaway["channel_id"])
    if channel:
        await channel.send(
            f"🔄 A winner was replaced because {reason}. New winner: <@{new_user_id}>\n"
            f"🎁 Prize: **{giveaway['prize']}**\n"
            f"⏰ They have {giveaway['claim_minutes']} minutes to claim."
        )


async def finish_giveaway(giveaway, channel=None):
    if giveaway["status"] != "active":
        return False
    guild = bot.get_guild(giveaway["guild_id"])
    if guild is None:
        return False
    giveaway["status"] = "ended"
    giveaway["ends_at"] = utcnow()
    winners = choose_winners(guild, giveaway, giveaway["winner_count"])
    giveaway["winners"] = winners
    giveaway["claims"] = {user_id: "pending" for user_id in winners}
    await edit_giveaway_message(giveaway, disabled=True)
    if channel is None:
        channel = bot.get_channel(giveaway["channel_id"])
    if channel:
        claim_embed = discord.Embed(title="🎉 GIVEAWAY ENDED", color=discord.Color.gold())
        claim_embed.add_field(name="🎁 Prize", value=giveaway["prize"], inline=False)
        claim_embed.add_field(name="🏆 Winner(s)", value=mention_ids(winners), inline=False)
        claim_embed.add_field(name="⏰ Claim Time", value=f"{giveaway['claim_minutes']} minutes per winner", inline=True)
        claim_embed.add_field(name="🛡️ Verification", value="A staff member must verify each winner.", inline=True)
        await channel.send(content=mention_ids(winners) if winners else None, embed=claim_embed)
    for user_id in winners:
        await add_winner_role(guild, user_id, giveaway)
        claim_tasks[(giveaway["key"], user_id)] = asyncio.create_task(claim_expiration(giveaway["key"], user_id))
    return True


async def refresh_invites(guild):
    try:
        current = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        invite_cache[guild.id] = None
        logger.warning("Invite tracking unavailable in guild %s", guild.id)
        return False
    invite_cache[guild.id] = {
        invite.code: {"uses": invite.uses or 0, "inviter_id": invite.inviter.id if invite.inviter else None}
        for invite in current
    }
    return True


async def check_requirements(interaction, giveaway):
    guild = interaction.guild
    member = interaction.user
    requirements = giveaway["requirements"]
    bypass_id = requirements.get("bypass_role_id")
    if bypass_id and any(role.id == bypass_id for role in member.roles):
        return None
    if requirements.get("invites") is not None:
        actual = invite_counts[guild.id][member.id]
        if actual < requirements["invites"]:
            return f"❌ You need {requirements['invites']} valid invites. Your invites: {actual}."
    role_id = requirements.get("role_id")
    if role_id and not any(role.id == role_id for role in member.roles):
        return f"❌ You need the <@&{role_id}> role."
    if requirements.get("booster") and member.premium_since is None:
        return "❌ You must be a Server Booster to enter this giveaway."
    return None


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_key, disabled=False):
        super().__init__(timeout=None)
        self.giveaway_key = giveaway_key
        enter = discord.ui.Button(label="Enter Giveaway", emoji="🎉", style=discord.ButtonStyle.success, custom_id=f"gw_enter:{giveaway_key}", disabled=disabled)
        participants = discord.ui.Button(label="Participants", emoji="👥", style=discord.ButtonStyle.secondary, custom_id=f"gw_participants:{giveaway_key}", disabled=disabled)
        enter.callback = self.enter_callback
        participants.callback = self.participants_callback
        self.add_item(enter)
        self.add_item(participants)

    async def enter_callback(self, interaction):
        giveaway = giveaways.get(self.giveaway_key)
        if not giveaway or giveaway["status"] != "active":
            await interaction.response.send_message("❌ This giveaway has ended.", ephemeral=True)
            return
        if interaction.user.id in giveaway["participants"]:
            await interaction.response.send_message("❌ You are already entered.", ephemeral=True)
            return
        error = await check_requirements(interaction, giveaway)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        giveaway["participants"].add(interaction.user.id)
        await interaction.response.send_message("🎉 You entered the giveaway!", ephemeral=True)
        await edit_giveaway_message(giveaway)

    async def participants_callback(self, interaction):
        giveaway = giveaways.get(self.giveaway_key)
        if not giveaway:
            await interaction.response.send_message("❌ Giveaway data is no longer available.", ephemeral=True)
            return
        await interaction.response.send_message(embed=participant_embed(giveaway, 0), view=ParticipantView(self.giveaway_key, 0), ephemeral=True)


def participant_embed(giveaway, page):
    guild = giveaway["guild"]
    ids = sorted(giveaway["participants"])
    page_size = 10
    pages = max(1, (len(ids) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    entries = ids[page * page_size:(page + 1) * page_size]
    lines = [f"{index}. {guild.get_member(user_id).mention if guild.get_member(user_id) else f'<@{user_id}>'}" for index, user_id in enumerate(entries, page * page_size + 1)]
    embed = discord.Embed(title="👥 Giveaway Participants", description="\n".join(lines) if lines else "No participants yet.", color=discord.Color.blurple())
    embed.set_footer(text=f"Page {page + 1}/{pages}")
    return embed


class ParticipantView(discord.ui.View):
    def __init__(self, giveaway_key, page):
        super().__init__(timeout=120)
        self.giveaway_key = giveaway_key
        self.page = page
        previous = discord.ui.Button(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary)
        next_button = discord.ui.Button(label="Next", emoji="▶", style=discord.ButtonStyle.secondary)
        previous.callback = self.previous_callback
        next_button.callback = self.next_callback
        self.add_item(previous)
        self.add_item(next_button)

    async def update(self, interaction, page):
        giveaway = giveaways.get(self.giveaway_key)
        if not giveaway:
            await interaction.response.edit_message(content="Giveaway data is no longer available.", embed=None, view=None)
            return
        await interaction.response.edit_message(embed=participant_embed(giveaway, page), view=ParticipantView(self.giveaway_key, page))

    async def previous_callback(self, interaction):
        await self.update(interaction, self.page - 1)

    async def next_callback(self, interaction):
        await self.update(interaction, self.page + 1)


class RewardView(discord.ui.View):
    def __init__(self, reward_id, disabled=False):
        super().__init__(timeout=None)
        self.reward_id = reward_id
        button = discord.ui.Button(label="Claim Reward", emoji="🎁", style=discord.ButtonStyle.success, custom_id=f"invite_claim:{reward_id}", disabled=disabled)
        button.callback = self.claim_callback
        self.add_item(button)

    async def claim_callback(self, interaction):
        reward = rewards.get(self.reward_id)
        if not reward or not reward["active"]:
            await interaction.response.send_message("❌ This reward is no longer active.", ephemeral=True)
            return
        if interaction.user.id in reward["claimed"]:
            await interaction.response.send_message("❌ You have already claimed this reward.", ephemeral=True)
            return
        count = invite_counts[interaction.guild.id][interaction.user.id]
        if count < reward["required"]:
            await interaction.response.send_message(
                f"❌ You need {reward['required']} invites.\nYour invites: {count}", ephemeral=True
            )
            return
        reward["claimed"].add(interaction.user.id)
        role_result = ""
        role = interaction.guild.get_role(reward.get("role_id")) if reward.get("role_id") else None
        if role:
            try:
                await interaction.user.add_roles(role, reason="Invite reward claimed")
                role_result = f"\n🏅 Role awarded: {role.mention}"
            except (discord.Forbidden, discord.HTTPException):
                role_result = "\n⚠️ The reward role could not be assigned."
        await interaction.response.send_message(f"🎉 Reward Claimed Successfully!{role_result}", ephemeral=True)


class GiveawayBot(commands.Bot):
    async def setup_hook(self):
        if TEST_GUILD_ID:
            guild = discord.Object(id=TEST_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Synced application commands to test guild %s", TEST_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Synced application commands globally")


bot = GiveawayBot(command_prefix=PREFIX, intents=intents, help_command=None)


async def create_giveaway(ctx, winners, duration, prize):
    if winners < 1 or winners > 100:
        raise ValueError("Winner count must be between 1 and 100.")
    seconds = parse_duration(duration) if isinstance(duration, str) else int(duration)
    key = secrets.token_hex(8)
    data = {
        "key": key,
        "guild_id": ctx.guild.id,
        "guild": ctx.guild,
        "channel_id": ctx.channel.id,
        "message_id": None,
        "prize": prize,
        "winner_count": winners,
        "ends_at": utcnow() + timedelta(seconds=seconds),
        "participants": set(),
        "requirements": {"invites": None, "role_id": None, "booster": False, "bypass_role_id": None},
        "winner_role_id": setting(ctx.guild.id, "winner_role_id"),
        "claim_minutes": setting(ctx.guild.id, "claim_minutes", 30),
        "status": "active",
        "winners": [],
        "claims": {},
        "history": set(),
    }
    giveaways[key] = data
    message = await ctx.send(embed=giveaway_embed(data), view=GiveawayView(key))
    data["message_id"] = message.id
    asyncio.create_task(end_at_time(key, seconds))
    return data


async def end_at_time(key, seconds):
    await asyncio.sleep(seconds)
    giveaway = giveaways.get(key)
    if giveaway and giveaway["status"] == "active":
        await finish_giveaway(giveaway)


@bot.event
async def on_ready():
    if not getattr(bot, "invite_cache_initialized", False):
        bot.invite_cache_initialized = True
        for guild in bot.guilds:
            await refresh_invites(guild)
    logger.info("Logged in as %s", bot.user)


@bot.event
async def on_invite_create(invite):
    cache = invite_cache.setdefault(invite.guild.id, {})
    if cache is not None:
        cache[invite.code] = {"uses": invite.uses or 0, "inviter_id": invite.inviter.id if invite.inviter else None}


@bot.event
async def on_invite_delete(invite):
    cache = invite_cache.get(invite.guild.id)
    if cache is not None:
        cache.pop(invite.code, None)


@bot.event
async def on_member_join(member):
    cache = invite_cache.get(member.guild.id)
    if cache is None:
        await refresh_invites(member.guild)
        return
    try:
        current = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return
    old_cache = cache.copy()
    new_cache = {
        invite.code: {"uses": invite.uses or 0, "inviter_id": invite.inviter.id if invite.inviter else None}
        for invite in current
    }
    used = None
    for code, record in new_cache.items():
        old_uses = old_cache.get(code, {}).get("uses", 0)
        if record["uses"] > old_uses:
            used = record
            break
    invite_cache[member.guild.id] = new_cache
    if used and used["inviter_id"] and used["inviter_id"] != member.id:
        inviter = member.guild.get_member(used["inviter_id"])
        if inviter and not inviter.bot:
            invite_counts[member.guild.id][inviter.id] += 1


@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="SAB Bot Help", description=f"Prefix: `{PREFIX}`", color=discord.Color.blurple())
    embed.add_field(name="🎉 Giveaways", value="`create <winners> <duration> <prize>`\n`end <message_id>`\n`reroll <message_id>`\n`claim <message_id> [@winner]`\n`participants <message_id>`\n`requirement <message_id> <type> <value>`\n`claimtime <message_id> <minutes>`", inline=False)
    embed.add_field(name="🎁 Invite Rewards", value="`invite reward <invites> <prize>`\n`invite end <reward_id>`\n`rewardrole <reward_id> @role`", inline=False)
    embed.add_field(name="👥 Invites", value="`invites [@user]`\n`leaderboard`\n`invite add @user <amount>`\n`invite set @user <amount>`", inline=False)
    embed.add_field(name="📋 Templates", value="`template create <name> <winners> <duration> <prize>`\n`template list`\n`template delete <name>`\n`template start <name>`", inline=False)
    embed.add_field(name="⚙️ Setup", value="`setup`\n`winnerrole @role`", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="create")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def create_command(ctx, winners: int, duration: str, *, prize: str):
    try:
        await create_giveaway(ctx, winners, duration, prize)
    except ValueError as error:
        await ctx.send(f"❌ {error}")


@bot.command(name="end")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def end_command(ctx, message_id: int):
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway:
        await ctx.send("❌ Giveaway not found in this server.")
        return
    if await finish_giveaway(giveaway, ctx.channel):
        await ctx.send("✅ Giveaway ended.", delete_after=5)
    else:
        await ctx.send("❌ This giveaway has already ended.")


@bot.command(name="reroll")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def reroll_command(ctx, message_id: int):
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway or giveaway["status"] != "ended":
        await ctx.send("❌ Ended giveaway not found.")
        return
    old = next((user_id for user_id in giveaway["winners"] if giveaway["claims"].get(user_id) != "claimed"), None)
    if old is None:
        await ctx.send("❌ There is no unclaimed winner to reroll.")
        return
    cancel_claim_task(giveaway["key"], old)
    await remove_winner_role(ctx.guild, old, giveaway)
    await replace_winner(giveaway, old, "manual reroll")
    new_winner = giveaway["winners"][giveaway["winners"].index(old)] if old in giveaway["winners"] else giveaway["winners"][-1]
    await ctx.send(f"🔄 Giveaway Rerolled!\n🎉 New Winner: <@{new_winner}>\n🎁 Prize: **{giveaway['prize']}**")


@bot.command(name="participants")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def participants_command(ctx, message_id: int):
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway:
        await ctx.send("❌ Giveaway not found in this server.")
        return
    await ctx.send(embed=participant_embed(giveaway, 0), view=ParticipantView(giveaway["key"], 0))


@bot.command(name="claimtime")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def claimtime_command(ctx, message_id: int, minutes: int):
    if minutes < 1 or minutes > 10080:
        await ctx.send("❌ Claim time must be between 1 and 10080 minutes.")
        return
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway:
        await ctx.send("❌ Giveaway not found in this server.")
        return
    giveaway["claim_minutes"] = minutes
    await edit_giveaway_message(giveaway, giveaway["status"] != "active")
    await ctx.send(f"✅ Claim time set to {minutes} minutes.")


@bot.command(name="requirement")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def requirement_command(ctx, message_id: int, requirement_type: str, *, value: str):
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway or giveaway["status"] != "active":
        await ctx.send("❌ Active giveaway not found.")
        return
    kind = requirement_type.casefold()
    try:
        if kind == "invites":
            amount = int(value)
            if amount < 0:
                raise ValueError
            giveaway["requirements"]["invites"] = amount
        elif kind == "role":
            role = await commands.RoleConverter().convert(ctx, value)
            giveaway["requirements"]["role_id"] = role.id
        elif kind == "booster":
            if value.casefold() not in {"yes", "no", "true", "false", "on", "off"}:
                raise ValueError
            giveaway["requirements"]["booster"] = value.casefold() in {"yes", "true", "on"}
        elif kind == "bypass":
            role = await commands.RoleConverter().convert(ctx, value)
            giveaway["requirements"]["bypass_role_id"] = role.id
        else:
            await ctx.send("❌ Requirement type must be `invites`, `role`, `booster`, or `bypass`.")
            return
    except (ValueError, commands.BadArgument):
        await ctx.send("❌ Invalid requirement value.")
        return
    await edit_giveaway_message(giveaway)
    await ctx.send("✅ Giveaway requirements updated.")


@bot.command(name="setup")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def setup_command(ctx):
    role = discord.utils.find(lambda item: item.name.casefold() == "giveaway winner", ctx.guild.roles)
    if role is None:
        try:
            role = await ctx.guild.create_role(name="giveaway winner", reason="SAB giveaway setup")
        except (discord.Forbidden, discord.HTTPException):
            await ctx.send("❌ I could not create `giveaway winner`. Give me Manage Roles and try again.")
            return
    guild_settings[ctx.guild.id]["winner_role_id"] = role.id
    guild_settings[ctx.guild.id].setdefault("claim_minutes", 30)
    await ctx.send(f"✅ Setup complete. Winner role: {role.mention}\nDefault claim time: 30 minutes.")


@bot.command(name="winnerrole")
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def winnerrole_command(ctx, role: discord.Role):
    guild_settings[ctx.guild.id]["winner_role_id"] = role.id
    await ctx.send(f"✅ Giveaway winner role set to {role.mention}.")


@bot.command(name="invites")
@commands.guild_only()
async def invites_command(ctx, member: Optional[discord.Member] = None):
    member = member or ctx.author
    count = invite_counts[ctx.guild.id][member.id]
    await ctx.send(f"👥 {member.mention} has {count} valid invites.")


@bot.command(name="leaderboard")
@commands.guild_only()
async def leaderboard_command(ctx):
    counts = invite_counts[ctx.guild.id]
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[index] if index < 3 else f'{index + 1}.'} <@{user_id}> — {amount} invites" for index, (user_id, amount) in enumerate(top)]
    embed = discord.Embed(title="🏆 Invite Leaderboard", description="\n".join(lines) if lines else "No invite data yet.", color=discord.Color.gold())
    await ctx.send(embed=embed)


@bot.group(name="invite", invoke_without_command=True)
@commands.guild_only()
async def invite_group(ctx):
    await ctx.send(f"Use `{PREFIX} help` to view invite commands.", delete_after=8)


@invite_group.command(name="add")
@commands.has_permissions(manage_guild=True)
async def invite_add(ctx, member: discord.Member, amount: int):
    if amount < 0:
        await ctx.send("❌ Amount cannot be negative.")
        return
    invite_counts[ctx.guild.id][member.id] += amount
    await ctx.send(f"✅ Added {amount} invites to {member.mention}.")


@invite_group.command(name="set")
@commands.has_permissions(manage_guild=True)
async def invite_set(ctx, member: discord.Member, amount: int):
    if amount < 0:
        await ctx.send("❌ Amount cannot be negative.")
        return
    invite_counts[ctx.guild.id][member.id] = amount
    await ctx.send(f"✅ Set {member.mention}'s invites to {amount}.")


@invite_group.command(name="reward")
@commands.has_permissions(manage_guild=True)
async def invite_reward(ctx, required_invites: int, *, prize: str):
    global next_reward_id
    if required_invites < 1 or not prize.strip():
        await ctx.send("❌ Required invites and reward text must be valid.")
        return
    reward_id = next_reward_id
    next_reward_id += 1
    reward = {"id": reward_id, "guild_id": ctx.guild.id, "channel_id": ctx.channel.id, "required": required_invites, "prize": prize, "claimed": set(), "role_id": None, "active": True}
    rewards[reward_id] = reward
    await ctx.send(embed=reward_embed(reward), view=RewardView(reward_id))


@invite_group.command(name="end")
@commands.has_permissions(manage_guild=True)
async def invite_end(ctx, reward_id: int):
    reward = rewards.get(reward_id)
    if not reward or reward["guild_id"] != ctx.guild.id:
        await ctx.send("❌ Reward not found in this server.")
        return
    reward["active"] = False
    try:
        channel = bot.get_channel(reward["channel_id"]) or await bot.fetch_channel(reward["channel_id"])
        message = await channel.fetch_message(reward.get("message_id")) if reward.get("message_id") else None
        if message:
            await message.edit(view=RewardView(reward_id, disabled=True))
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass
    await ctx.send("✅ Invite reward disabled.")


@bot.command(name="rewardrole")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def rewardrole_command(ctx, reward_id: int, role: discord.Role):
    reward = rewards.get(reward_id)
    if not reward or reward["guild_id"] != ctx.guild.id:
        await ctx.send("❌ Reward not found in this server.")
        return
    reward["role_id"] = role.id
    await ctx.send(f"✅ Reward role set to {role.mention}.")


@bot.group(name="template", invoke_without_command=True)
@commands.guild_only()
async def template_group(ctx):
    await ctx.send(f"Use `{PREFIX} help` to view template commands.", delete_after=8)


@template_group.command(name="create")
@commands.has_permissions(manage_guild=True)
async def template_create(ctx, name: str, winners: int, duration: str, *, prize: str):
    name = name.casefold()
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", name):
        await ctx.send("❌ Template names may contain only letters, numbers, `_`, and `-`.")
        return
    try:
        seconds = parse_duration(duration)
    except ValueError as error:
        await ctx.send(f"❌ {error}")
        return
    if winners < 1 or winners > 100:
        await ctx.send("❌ Winner count must be between 1 and 100.")
        return
    templates[ctx.guild.id][name] = {"winners": winners, "seconds": seconds, "prize": prize}
    await ctx.send(f"✅ Template `{name}` saved.")


@template_group.command(name="list")
async def template_list(ctx):
    server_templates = templates[ctx.guild.id]
    if not server_templates:
        await ctx.send("📋 No giveaway templates have been created.")
        return
    lines = [f"`{name}` — {data['winners']} winner(s), {format_duration(data['seconds'])}, {data['prize']}" for name, data in sorted(server_templates.items())]
    await ctx.send(embed=discord.Embed(title="📋 Giveaway Templates", description="\n".join(lines), color=discord.Color.blurple()))


@template_group.command(name="delete")
@commands.has_permissions(manage_guild=True)
async def template_delete(ctx, name: str):
    name = name.casefold()
    if templates[ctx.guild.id].pop(name, None) is None:
        await ctx.send("❌ Template not found.")
        return
    await ctx.send(f"✅ Template `{name}` deleted.")


@template_group.command(name="start")
@commands.has_permissions(manage_guild=True)
async def template_start(ctx, name: str):
    data = templates[ctx.guild.id].get(name.casefold())
    if not data:
        await ctx.send("❌ Template not found.")
        return
    await create_giveaway(ctx, data["winners"], data["seconds"], data["prize"])


@bot.command(name="claim")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def claim_command(ctx, message_id: int, winner: Optional[discord.Member] = None):
    giveaway = next((item for item in giveaways.values() if item["guild_id"] == ctx.guild.id and item["message_id"] == message_id), None)
    if not giveaway or giveaway["status"] != "ended":
        await ctx.send("❌ Ended giveaway not found.")
        return
    pending = [user_id for user_id in giveaway["winners"] if giveaway["claims"].get(user_id) == "pending"]
    if winner:
        user_id = winner.id
        if user_id not in pending:
            await ctx.send("❌ That user is not a pending winner for this giveaway.")
            return
    elif len(pending) == 1:
        user_id = pending[0]
    elif len(pending) > 1:
        await ctx.send(f"❌ Multiple winners are pending. Use `{PREFIX} claim {message_id} @winner`.")
        return
    else:
        await ctx.send("❌ There are no pending winners.")
        return
    giveaway["claims"][user_id] = "claimed"
    cancel_claim_task(giveaway["key"], user_id)
    await add_winner_role(ctx.guild, user_id, giveaway)
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    await ctx.send(f"🏆 Giveaway Claimed Successfully!\n\n🎁 Prize: **{giveaway['prize']}**\n🏆 Winner: <@{user_id}>\n🛡️ Verified By: {ctx.author.mention}")


@bot.tree.command(name="invites", description="Show your valid invite count or another member's count.")
@app_commands.describe(member="The member to check")
async def slash_invites(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    count = invite_counts[interaction.guild.id][target.id]
    await interaction.response.send_message(f"👥 {target.mention} has {count} valid invites.")


@bot.tree.command(name="leaderboard", description="Show the top ten inviters.")
async def slash_leaderboard(interaction: discord.Interaction):
    counts = invite_counts[interaction.guild.id]
    top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:10]
    lines = [f"{index + 1}. <@{user_id}> — {amount} invites" for index, (user_id, amount) in enumerate(top)]
    await interaction.response.send_message(embed=discord.Embed(title="🏆 Invite Leaderboard", description="\n".join(lines) if lines else "No invite data yet.", color=discord.Color.gold()))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You do not have permission to use this command.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ This command can only be used in a server.")
        elif isinstance(error, commands.MissingRole):
            await ctx.send("❌ You do not have the required role.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ I could not understand one of those arguments.")
    elif isinstance(error, commands.MissingChannel):
        await ctx.send("❌ The required channel is missing or unavailable.")
    elif isinstance(error, discord.Forbidden):
        await ctx.send("❌ I do not have permission to complete that action.")
    elif isinstance(error, discord.HTTPException):
        await ctx.send("❌ Discord rejected that request. Please try again shortly.")
    else:
        logger.exception("Unhandled command error", exc_info=error)
        await ctx.send("❌ An unexpected error occurred while processing that command.")


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set.")

bot.run(TOKEN)