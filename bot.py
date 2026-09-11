import os
import re
import random
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId

load_dotenv()

TOKEN = os.getenv("TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
PREFIX = os.getenv("PREFIX", "g.w")
DATABASE_NAME = os.getenv("DATABASE_NAME", "giveaway_bot")
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)

if not TOKEN:
    raise RuntimeError("TOKEN is missing from .env")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is missing from .env")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[DATABASE_NAME]

giveaways = db["giveaways"]
participants = db["giveaway_participants"]
winners = db["giveaway_winners"]
extra_roles = db["extra_roles"]
templates = db["giveaway_templates"]
invites = db["invites"]
invite_sources = db["invite_sources"]
invite_rewards = db["invite_rewards"]
invite_reward_claims = db["invite_reward_claims"]
settings = db["settings"]

WHITE = discord.Color.from_rgb(255, 255, 255)


def now():
    return datetime.now(timezone.utc)


def parse_duration(value: str):
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", value.lower())
    if not m:
        raise ValueError("Use formats like `30s`, `10m`, `2h`, `1d`, `1w`.")
    amount = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return timedelta(seconds=amount * seconds)


def discord_time(dt):
    return f"<t:{int(dt.timestamp())}:R>"


def role_ids_for_member(member: discord.Member):
    return {r.id for r in member.roles}


def get_entry_weight(guild_id, member):
    role_ids = role_ids_for_member(member)
    docs = extra_roles.find({"guild_id": guild_id})
    weight = 1
    for d in docs:
        if d["role_id"] in role_ids:
            weight += int(d.get("extra_entries", 0))
    return max(1, weight)


def invite_count(guild_id, user_id):
    doc = invites.find_one({"guild_id": guild_id, "user_id": user_id})
    return int(doc.get("count", 0)) if doc else 0


def is_staff(member: discord.Member):
    if member.guild_permissions.administrator:
        return True
    configured = settings.find_one({"guild_id": member.guild.id})
    role_id = configured.get("manager_role_id") if configured else None
    return bool(role_id and any(r.id == role_id for r in member.roles))


def requirement_text(guild, g):
    parts = []
    if g.get("min_invites", 0):
        parts.append(f"📨 {g['min_invites']} invite(s)")
    if g.get("required_role_id"):
        role = guild.get_role(g["required_role_id"])
        parts.append(f"🎭 {role.mention if role else 'Required Role'}")
    if g.get("booster_required"):
        parts.append("💎 Server Booster")
    if not parts:
        return "None"
    return "\n".join(f"> {x}" for x in parts)


def extra_text(guild_id):
    docs = list(extra_roles.find({"guild_id": guild_id}).sort("extra_entries", 1))
    if not docs:
        return "None configured"
    lines = []
    for d in docs:
        role = guild_role_cache.get((guild_id, d["role_id"]))
        name = role.name if role else f"Role {d['role_id']}"
        lines.append(f"> {name} — +{d['extra_entries']}")
    return "\n".join(lines)


guild_role_cache = {}


async def fetch_member(guild, user_id):
    member = guild.get_member(user_id)
    if member:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


async def eligible(guild, member, g):
    if member.bot:
        return False, "Bots cannot enter."
    bypass = g.get("bypass_role_id")
    if bypass and any(r.id == bypass for r in member.roles):
        return True, None
    if g.get("min_invites", 0) > invite_count(guild.id, member.id):
        return False, f"You need {g['min_invites']} invite(s)."
    req = g.get("required_role_id")
    if req and not any(r.id == req for r in member.roles):
        return False, "You do not have the required role."
    if g.get("booster_required") and not member.premium_since:
        return False, "You must be a server booster."
    return True, None


def choose_weighted(guild, ids, g):
    pool = []
    for uid in ids:
        m = guild.get_member(uid)
        if not m:
            continue
        pool.extend([uid] * get_entry_weight(guild.id, m))
    return random.choice(pool) if pool else None


def build_giveaway_embed(guild, g, participant_count):
    ends = datetime.fromtimestamp(g["ends_at"], timezone.utc)
    embed = discord.Embed(title="🎉 GIVEAWAY", color=WHITE)
    embed.add_field(name="🎁 Prize", value=f"> {g['prize']}", inline=False)
    embed.add_field(name="🏆 Winners", value=f"> {g['winner_count']}", inline=True)
    embed.add_field(name="⏰ Ends", value=f"> {discord_time(ends)}", inline=True)
    embed.add_field(name="👥 Participants", value=f"> {participant_count}", inline=True)
    embed.add_field(name="🎟️ Extra Entries", value=extra_text(guild.id), inline=False)
    embed.add_field(name="📋 Requirements", value=requirement_text(guild, g), inline=False)
    embed.set_footer(text=f"Giveaway ID: {g['_id']}")
    return embed


async def refresh_giveaway_message(guild, g):
    channel = guild.get_channel(g["channel_id"])
    if not channel:
        return
    try:
        msg = await channel.fetch_message(g["message_id"])
        count = participants.count_documents({"giveaway_id": str(g["_id"])})
        await msg.edit(embed=build_giveaway_embed(guild, g, count), view=GiveawayView(str(g["_id"])))
    except Exception:
        pass


class ParticipantsView(discord.ui.View):
    def __init__(self, giveaway_id, page=0):
        super().__init__(timeout=120)
        self.giveaway_id = giveaway_id
        self.page = page

    async def make_embed(self, interaction):
        ids = [x["user_id"] for x in participants.find({"giveaway_id": self.giveaway_id}).sort("joined_at", 1)]
        per_page = 10
        total_pages = max(1, (len(ids) + per_page - 1) // per_page)
        page = min(self.page, total_pages - 1)
        chunk = ids[page * per_page:(page + 1) * per_page]
        lines = []
        for i, uid in enumerate(chunk, page * per_page + 1):
            lines.append(f"{i}. <@{uid}>")
        embed = discord.Embed(
            title="👥 Giveaway Participants",
            description="\n".join(lines) if lines else "No one has joined yet.",
            color=WHITE,
        )
        embed.set_footer(text=f"Page {page + 1}/{total_pages} • Total: {len(ids)}")
        return embed, total_pages

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, button):
        if self.page > 0:
            self.page -= 1
        embed, _ = await self.make_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        embed, total = await self.make_embed(interaction)
        if self.page + 1 < total:
            self.page += 1
        embed, _ = await self.make_embed(interaction)
        await interaction.response.edit_message(embed=embed, view=self)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.primary)
    async def enter(self, interaction, button):
        g = giveaways.find_one({"_id": ObjectId(self.giveaway_id)})
        if not g or g.get("status") != "active":
            return await interaction.response.send_message("❌ This giveaway is not active.", ephemeral=True)

        ok, reason = await eligible(interaction.guild, interaction.user, g)
        if not ok:
            return await interaction.response.send_message(f"❌ {reason}", ephemeral=True)

        exists = participants.find_one({"giveaway_id": self.giveaway_id, "user_id": interaction.user.id})
        if exists:
            return await interaction.response.send_message("ℹ️ You already joined this giveaway.", ephemeral=True)

        participants.insert_one({
            "giveaway_id": self.giveaway_id,
            "user_id": interaction.user.id,
            "joined_at": now(),
        })
        await refresh_giveaway_message(interaction.guild, g)
        await interaction.response.send_message("🎉 You entered the giveaway!", ephemeral=True)

    @discord.ui.button(label="👥 Participants", style=discord.ButtonStyle.secondary)
    async def participant_button(self, interaction, button):
        view = ParticipantsView(self.giveaway_id)
        embed, _ = await view.make_embed(interaction)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class WinnerClaimView(discord.ui.View):
    def __init__(self, giveaway_id, user_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id
        self.user_id = user_id

    @discord.ui.button(label="🏆 Claim Prize", style=discord.ButtonStyle.success)
    async def claim(self, interaction, button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This claim button is only for the winner.", ephemeral=True)
        doc = winners.find_one({"giveaway_id": self.giveaway_id, "user_id": self.user_id})
        if not doc or doc.get("status") != "pending":
            return await interaction.response.send_message("❌ This claim is no longer available.", ephemeral=True)
        if now().timestamp() > doc["deadline_at"]:
            return await interaction.response.send_message("⏰ Your claim time has expired.", ephemeral=True)
        winners.update_one({"_id": doc["_id"]}, {"$set": {"status": "claimed", "claimed_at": now()}})
        await interaction.response.send_message("🏆 Giveaway claimed successfully! Please continue with the staff ticket.", ephemeral=True)


class InviteRewardView(discord.ui.View):
    def __init__(self, reward_id):
        super().__init__(timeout=None)
        self.reward_id = reward_id

    @discord.ui.button(label="🎁 Claim Reward", style=discord.ButtonStyle.success)
    async def claim(self, interaction, button):
        reward = invite_rewards.find_one({"_id": ObjectId(self.reward_id)})
        if not reward or not reward.get("active", True):
            return await interaction.response.send_message("❌ This invite reward is no longer active.", ephemeral=True)
        if now().timestamp() > reward["ends_at"]:
            return await interaction.response.send_message("⏰ This invite reward has expired.", ephemeral=True)
        count = invite_count(interaction.guild.id, interaction.user.id)
        if count < reward["required_invites"]:
            return await interaction.response.send_message(
                f"❌ You need {reward['required_invites']} valid invites. You currently have {count}.", ephemeral=True
            )
        already = invite_reward_claims.find_one({"reward_id": self.reward_id, "user_id": interaction.user.id})
        if already:
            return await interaction.response.send_message("❌ You already claimed this reward.", ephemeral=True)

        invite_reward_claims.insert_one({
            "reward_id": self.reward_id,
            "guild_id": interaction.guild.id,
            "user_id": interaction.user.id,
            "claimed_at": now(),
        })

        role_id = reward.get("reward_role_id")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            try:
                await interaction.user.add_roles(role, reason="Invite reward claimed")
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            f"🎁 **Invite Reward Claimed!**\nPrize: **{reward['prize']}**", ephemeral=True
        )


async def finish_giveaway(g):
    if g.get("status") not in ("active", "claiming"):
        return
    guild = bot.get_guild(g["guild_id"])
    if not guild:
        return

    old_pending = list(winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}))
    for w in old_pending:
        if w["deadline_at"] <= now().timestamp():
            winners.update_one({"_id": w["_id"]}, {"$set": {"status": "expired"}})
            member = await fetch_member(guild, w["user_id"])
            role = guild.get_role(g.get("winner_role_id")) if g.get("winner_role_id") else None
            if member and role:
                try:
                    await member.remove_roles(role, reason="Giveaway claim expired")
                except discord.Forbidden:
                    pass

    g = giveaways.find_one({"_id": g["_id"]})
    if g["status"] == "active" and g["ends_at"] > now().timestamp():
        return

    claimed_or_pending = list(winners.find({
        "giveaway_id": str(g["_id"]),
        "status": {"$in": ["pending", "claimed"]}
    }))
    current_ids = {w["user_id"] for w in claimed_or_pending}
    target = int(g["winner_count"])

    if len(current_ids) < target:
        joined = [x["user_id"] for x in participants.find({"giveaway_id": str(g["_id"])})]
        excluded = {x["user_id"] for x in winners.find({"giveaway_id": str(g["_id"])})}
        candidates = [uid for uid in joined if uid not in excluded]
        while len(current_ids) < target and candidates:
            eligible_ids = []
            for uid in candidates:
                m = guild.get_member(uid)
                if m:
                    ok, _ = await eligible(guild, m, g)
                    if ok:
                        eligible_ids.append(uid)
            if not eligible_ids:
                break
            uid = choose_weighted(guild, eligible_ids, g)
            if uid is None:
                break
            deadline = now() + timedelta(seconds=int(g["claim_seconds"]))
            winners.insert_one({
                "giveaway_id": str(g["_id"]),
                "guild_id": guild.id,
                "user_id": uid,
                "status": "pending",
                "deadline_at": deadline.timestamp(),
                "created_at": now(),
            })
            current_ids.add(uid)
            candidates.remove(uid)
            member = guild.get_member(uid)
            role = guild.get_role(g.get("winner_role_id")) if g.get("winner_role_id") else None
            if member and role:
                try:
                    await member.add_roles(role, reason="Giveaway winner")
                except discord.Forbidden:
                    pass

    giveaways.update_one({"_id": g["_id"]}, {"$set": {"status": "claiming"}})
    await refresh_giveaway_message(guild, g)

    channel = guild.get_channel(g["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(g["message_id"])
            active = list(winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}))
            if active:
                mentions = ", ".join(f"<@{x['user_id']}>" for x in active)
                await msg.reply(f"🎉 **Giveaway ended!** Winner(s): {mentions}\nUse the button above to claim within the claim time.")
                for x in active:
                    await msg.reply(view=WinnerClaimView(str(g["_id"]), x["user_id"]))
            else:
                await msg.reply("❌ No eligible winners could be selected.")
        except Exception:
            pass


class GiveawayGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="giveaway", description="Giveaway management commands")

    @app_commands.command(name="start", description="Start a giveaway")
    @app_commands.describe(
        prize="Giveaway prize",
        duration="Example: 10m, 2h, 1d",
        winners="Number of winners",
        claim_time="How long winners have to claim",
        min_invites="Minimum valid invites",
        required_role="Required role",
        bypass_role="Role that bypasses requirements",
        booster_required="Require server booster",
        winner_role="Role given to winners",
        template="Optional saved template name",
    )
    async def start(self, interaction: discord.Interaction, prize: str, duration: str, winners: int = 1,
                    claim_time: str = "10m", min_invites: int = 0,
                    required_role: discord.Role | None = None,
                    bypass_role: discord.Role | None = None,
                    booster_required: bool = False,
                    winner_role: discord.Role | None = None,
                    template: str | None = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ You need giveaway manager permissions.", ephemeral=True)

        if template:
            t = templates.find_one({"guild_id": interaction.guild.id, "name": template.lower()})
            if not t:
                return await interaction.response.send_message("❌ Template not found.", ephemeral=True)
            prize = t["prize"]
            duration = t["duration"]
            winners = t["winner_count"]
            claim_time = t["claim_time"]
            min_invites = t.get("min_invites", 0)
            required_role = interaction.guild.get_role(t["required_role_id"]) if t.get("required_role_id") else None
            bypass_role = interaction.guild.get_role(t["bypass_role_id"]) if t.get("bypass_role_id") else None
            booster_required = t.get("booster_required", False)
            winner_role = interaction.guild.get_role(t["winner_role_id"]) if t.get("winner_role_id") else None

        try:
            end_at = now() + parse_duration(duration)
            claim_seconds = int(parse_duration(claim_time).total_seconds())
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        if winners < 1:
            return await interaction.response.send_message("❌ Winners must be at least 1.", ephemeral=True)

        await interaction.response.defer()

        g = {
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message_id": 0,
            "prize": prize,
            "winner_count": winners,
            "ends_at": end_at.timestamp(),
            "claim_seconds": claim_seconds,
            "winner_role_id": winner_role.id if winner_role else None,
            "required_role_id": required_role.id if required_role else None,
            "bypass_role_id": bypass_role.id if bypass_role else None,
            "min_invites": min_invites,
            "booster_required": booster_required,
            "status": "active",
            "created_by": interaction.user.id,
            "created_at": now(),
        }
        result = giveaways.insert_one(g)
        g["_id"] = result.inserted_id

        msg = await interaction.channel.send(
            embed=build_giveaway_embed(interaction.guild, g, 0),
            view=GiveawayView(str(result.inserted_id))
        )
        giveaways.update_one({"_id": result.inserted_id}, {"$set": {"message_id": msg.id}})
        await interaction.followup.send(f"✅ Giveaway created: {msg.jump_url}", ephemeral=True)

    @app_commands.command(name="end", description="End a giveaway now")
    async def end(self, interaction, message_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        g = giveaways.find_one({"guild_id": interaction.guild.id, "message_id": int(message_id)})
        if not g:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        giveaways.update_one({"_id": g["_id"]}, {"$set": {"ends_at": 0}})
        await finish_giveaway(g)
        await interaction.response.send_message("✅ Giveaway ended.", ephemeral=True)

    @app_commands.command(name="reroll", description="Reroll an expired/failed winner")
    async def reroll(self, interaction, message_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        g = giveaways.find_one({"guild_id": interaction.guild.id, "message_id": int(message_id)})
        if not g:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)

        pending = list(winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}))
        for w in pending:
            winners.update_one({"_id": w["_id"]}, {"$set": {"status": "expired"}})
            m = await fetch_member(interaction.guild, w["user_id"])
            role = interaction.guild.get_role(g.get("winner_role_id")) if g.get("winner_role_id") else None
            if m and role:
                try:
                    await m.remove_roles(role, reason="Manual reroll")
                except discord.Forbidden:
                    pass

        giveaways.update_one({"_id": g["_id"]}, {"$set": {"status": "active", "ends_at": 0}})
        await finish_giveaway(g)
        await interaction.response.send_message("🔄 Giveaway rerolled.", ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel a giveaway")
    async def cancel(self, interaction, message_id: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        g = giveaways.find_one({"guild_id": interaction.guild.id, "message_id": int(message_id)})
        if not g:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        giveaways.update_one({"_id": g["_id"]}, {"$set": {"status": "cancelled"}})
        await interaction.response.send_message("🛑 Giveaway cancelled.", ephemeral=True)


class TemplateGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="template", description="Giveaway templates")

    @app_commands.command(name="create", description="Create a giveaway template")
    async def create(self, interaction, name: str, prize: str, duration: str, winners: int = 1,
                      claim_time: str = "10m", min_invites: int = 0,
                      required_role: discord.Role | None = None,
                      bypass_role: discord.Role | None = None,
                      booster_required: bool = False,
                      winner_role: discord.Role | None = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        try:
            parse_duration(duration)
            parse_duration(claim_time)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        templates.update_one(
            {"guild_id": interaction.guild.id, "name": name.lower()},
            {"$set": {
                "guild_id": interaction.guild.id,
                "name": name.lower(),
                "prize": prize,
                "duration": duration,
                "winner_count": winners,
                "claim_time": claim_time,
                "min_invites": min_invites,
                "required_role_id": required_role.id if required_role else None,
                "bypass_role_id": bypass_role.id if bypass_role else None,
                "booster_required": booster_required,
                "winner_role_id": winner_role.id if winner_role else None,
            }},
            upsert=True
        )
        await interaction.response.send_message(f"✅ Template `{name}` saved.", ephemeral=True)

    @app_commands.command(name="list", description="List templates")
    async def list_templates(self, interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        docs = list(templates.find({"guild_id": interaction.guild.id}).sort("name", 1))
        await interaction.response.send_message(
            "\n".join(f"• `{d['name']}` — {d['prize']}" for d in docs) or "No templates.",
            ephemeral=True
        )

    @app_commands.command(name="delete", description="Delete a template")
    async def delete(self, interaction, name: str):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        result = templates.delete_one({"guild_id": interaction.guild.id, "name": name.lower()})
        await interaction.response.send_message("🗑️ Deleted." if result.deleted_count else "❌ Not found.", ephemeral=True)


class InviteRewardGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="invite-reward", description="Invite reward system")

    @app_commands.command(name="create", description="Create a claimable invite reward")
    async def create(self, interaction, prize: str, required_invites: int, duration: str,
                      reward_role: discord.Role | None = None):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("❌ No permission.", ephemeral=True)
        try:
            end = now() + parse_duration(duration)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        doc = {
            "guild_id": interaction.guild.id,
            "prize": prize,
            "required_invites": required_invites,
            "ends_at": end.timestamp(),
            "reward_role_id": reward_role.id if reward_role else None,
            "active": True,
            "created_at": now(),
        }
        result = invite_rewards.insert_one(doc)
        embed = discord.Embed(title="🎁 INVITE REWARD", color=WHITE)
        embed.add_field(name="🎁 Prize", value=f"> {prize}", inline=False)
        embed.add_field(name="📨 Required Invites", value=f"> {required_invites}", inline=True)
        embed.add_field(name="⏰ Ends", value=f"> {discord_time(end)}", inline=True)
        embed.add_field(name="💡 How to claim", value="Reach the required valid invites, then click the button.", inline=False)
        await interaction.channel.send(embed=embed, view=InviteRewardView(str(result.inserted_id)))
        await interaction.response.send_message("✅ Invite reward created.", ephemeral=True)


@bot.command(name="claim")
async def claim_command(ctx, message_id: int, member: discord.Member | None = None):
    """Staff command: g.w claim <giveaway_message_id> [winner]."""
    if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    g = giveaways.find_one({"guild_id": ctx.guild.id, "message_id": message_id})
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)

    pending = list(winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}))
    target = member.id if member else None
    w = next((x for x in pending if target is None or x["user_id"] == target), None)

    if not w:
        return await ctx.send("❌ No matching pending winner found.", delete_after=8)

    winners.update_one({"_id": w["_id"]}, {"$set": {"status": "claimed", "claimed_at": now(), "claimed_by": ctx.author.id}})
    await ctx.send(
        f"🏆 **Giveaway Claimed Successfully!**\n"
        f"🎁 Prize: **{g['prize']}**\n"
        f"👤 Winner: <@{w['user_id']}>\n"
        f"✅ Verified by: {ctx.author.mention}",
        delete_after=10
    )


@bot.command(name="end")
async def prefix_end(ctx, message_id: int):
    if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    g = giveaways.find_one({"guild_id": ctx.guild.id, "message_id": message_id})
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)
    giveaways.update_one({"_id": g["_id"]}, {"$set": {"ends_at": 0}})
    await finish_giveaway(g)


@bot.command(name="reroll")
async def prefix_reroll(ctx, message_id: int):
    if not isinstance(ctx.author, discord.Member) or not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    g = giveaways.find_one({"guild_id": ctx.guild.id, "message_id": message_id})
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)
    for w in winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}):
        winners.update_one({"_id": w["_id"]}, {"$set": {"status": "expired"}})
        m = await fetch_member(ctx.guild, w["user_id"])
        role = ctx.guild.get_role(g.get("winner_role_id")) if g.get("winner_role_id") else None
        if m and role:
            try:
                await m.remove_roles(role, reason="Reroll")
            except discord.Forbidden:
                pass
    giveaways.update_one({"_id": g["_id"]}, {"$set": {"status": "active", "ends_at": 0}})
    await finish_giveaway(g)


@bot.command(name="participants")
async def prefix_participants(ctx, message_id: int):
    g = giveaways.find_one({"guild_id": ctx.guild.id, "message_id": message_id})
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)
    view = ParticipantsView(str(g["_id"]))
    embed, _ = await view.make_embed(None)
    await ctx.send(embed=embed, view=view, delete_after=60)


@bot.command(name="template")
async def prefix_template_help(ctx):
    await ctx.send(
        "Use slash commands: `/template create`, `/template list`, `/template delete`.",
        delete_after=10
    )


@bot.command(name="setmanager")
async def set_manager(ctx, role: discord.Role):
    if not isinstance(ctx.author, discord.Member) or not ctx.author.guild_permissions.administrator:
        return
    settings.update_one({"guild_id": ctx.guild.id}, {"$set": {"manager_role_id": role.id}}, upsert=True)
    await ctx.send(f"✅ Giveaway manager role set to {role.mention}.", delete_after=8)


@bot.event
async def on_member_join(member):
    # Invite tracking based on usage delta.
    try:
        current = await member.guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return

    old_docs = {d["code"]: d.get("uses", 0) for d in invite_sources.find({"guild_id": member.guild.id})}
    changed = None
    for inv in current:
        old = old_docs.get(inv.code, 0)
        if inv.uses and inv.uses > old:
            changed = inv
            break

    for inv in current:
        invite_sources.update_one(
            {"guild_id": member.guild.id, "code": inv.code},
            {"$set": {"uses": inv.uses or 0, "inviter_id": inv.inviter.id if inv.inviter else None}},
            upsert=True
        )

    if changed and changed.inviter and changed.inviter.id != member.id:
        invites.update_one(
            {"guild_id": member.guild.id, "user_id": changed.inviter.id},
            {"$inc": {"count": 1}},
            upsert=True
        )


@bot.event
async def on_guild_join(guild):
    try:
        for inv in await guild.invites():
            invite_sources.update_one(
                {"guild_id": guild.id, "code": inv.code},
                {"$set": {"uses": inv.uses or 0, "inviter_id": inv.inviter.id if inv.inviter else None}},
                upsert=True
            )
    except Exception:
        pass


@bot.event
async def on_ready():
    for guild in bot.guilds:
        for role in guild.roles:
            guild_role_cache[(guild.id, role.id)] = role

    print(f"Logged in as {bot.user} ({bot.user.id})")

    try:
        if TEST_GUILD_ID:
            guild_obj = discord.Object(id=TEST_GUILD_ID)
            bot.tree.add_command(GiveawayGroup())
            bot.tree.add_command(TemplateGroup())
            bot.tree.add_command(InviteRewardGroup())
            await bot.tree.sync(guild=guild_obj)
        else:
            bot.tree.add_command(GiveawayGroup())
            bot.tree.add_command(TemplateGroup())
            bot.tree.add_command(InviteRewardGroup())
            await bot.tree.sync()
    except Exception as e:
        print("Slash command sync error:", e)

    if not giveaway_loop.is_running():
        giveaway_loop.start()


@tasks.loop(seconds=10)
async def giveaway_loop():
    # Expire pending claims and finish due giveaways.
    for g in giveaways.find({"status": {"$in": ["active", "claiming"]}}):
        if g.get("status") == "active" and g["ends_at"] <= now().timestamp():
            await finish_giveaway(g)

        for w in winners.find({"giveaway_id": str(g["_id"]), "status": "pending"}):
            if w["deadline_at"] <= now().timestamp():
                guild = bot.get_guild(g["guild_id"])
                if guild:
                    member = await fetch_member(guild, w["user_id"])
                    role = guild.get_role(g.get("winner_role_id")) if g.get("winner_role_id") else None
                    if member and role:
                        try:
                            await member.remove_roles(role, reason="Claim time expired")
                        except discord.Forbidden:
                            pass
                winners.update_one({"_id": w["_id"]}, {"$set": {"status": "expired"}})
                giveaways.update_one({"_id": g["_id"]}, {"$set": {"status": "active", "ends_at": 0}})
                await finish_giveaway(g)


@bot.tree.error
async def on_app_command_error(interaction, error):
    if interaction.response.is_done():
        await interaction.followup.send(f"❌ Error: {error}", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Error: {error}", ephemeral=True)


bot.run(TOKEN)
