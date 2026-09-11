# ============================================================
# SAB GIVEAWAY + INVITE BOT
# Python 3.11+ | discord.py 2.x
# NO MONGODB
# ============================================================

import os
import json
import random
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "g.w")
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)

DATA_FILE = "bot_data.json"

# ============================================================
# DATABASE
# ============================================================

DEFAULT_DATA = {
    "giveaways": {},
    "invite_rewards": {},
    "templates": {},
    "invites": {},
    "settings": {}
}

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for key, value in DEFAULT_DATA.items():
            data.setdefault(key, value.copy())

        return data

    except Exception:
        return {
            "giveaways": {},
            "invite_rewards": {},
            "templates": {},
            "invites": {},
            "settings": {}
        }


data = load_data()


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        # Some hosts have read-only storage.
        pass


# ============================================================
# INTENTS
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.invites = True


# ============================================================
# BOT
# ============================================================

bot = commands.Bot(
    command_prefix=PREFIX + " ",
    intents=intents,
    help_command=None
)


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc)


def timestamp(dt):
    return int(dt.timestamp())


def parse_duration(value):
    """
    Examples:
    10s
    10m
    2h
    7d
    """
    if not value:
        return None

    value = value.lower().strip()

    try:
        number = int(value[:-1])
        unit = value[-1]

        if unit == "s":
            return timedelta(seconds=number)
        if unit == "m":
            return timedelta(minutes=number)
        if unit == "h":
            return timedelta(hours=number)
        if unit == "d":
            return timedelta(days=number)

    except Exception:
        return None

    return None


def get_giveaway(gid):
    return data["giveaways"].get(str(gid))


def is_staff(member):
    return (
        member.guild_permissions.manage_guild
        or member.guild_permissions.administrator
    )


def has_role(member, role_id):
    return any(r.id == int(role_id) for r in member.roles)


def get_multiplier(member):
    """
    Highest multiplier wins.
    """

    multipliers = [
        ("16x Luck", 16),
        ("8x Luck", 8),
        ("4x Luck", 4),
        ("2x Luck", 2),
    ]

    for role_name, multiplier in multipliers:
        role = discord.utils.get(member.guild.roles, name=role_name)

        if role and role in member.roles:
            return multiplier

    return 1


def get_extra_entries(member):
    total = get_multiplier(member)

    role_values = {
        "Regular": 1,
        "Active": 2,
        "Most Active": 4,
    }

    for role_name, value in role_values.items():
        role = discord.utils.get(member.guild.roles, name=role_name)

        if role and role in member.roles:
            total += value

    return total


def get_valid_invites(guild_id, user_id):
    guild_data = data["invites"].get(str(guild_id), {})
    return int(guild_data.get(str(user_id), 0))


def requirement_text(gw):
    req = gw.get("requirements", {})
    result = []

    if req.get("invites", 0):
        result.append(f"👥 **{req['invites']} invites required**")

    if req.get("role"):
        result.append(f"<@&{req['role']}> required")

    if req.get("booster"):
        result.append("🚀 **Server Booster required**")

    return "\n".join(result) if result else "None"


# ============================================================
# EMBEDS
# ============================================================

def giveaway_embed(gw, ended=False):
    embed = discord.Embed(
        title="🎉 GIVEAWAY",
        color=discord.Color.from_rgb(255, 255, 255)
    )

    prize = gw["prize"]
    winners = gw["winners"]
    end_time = int(gw["end_time"])

    embed.add_field(
        name="🎁 Prize",
        value=f"**{prize}**",
        inline=False
    )

    embed.add_field(
        name="🏆 Winners",
        value=str(winners),
        inline=True
    )

    if ended:
        end_text = "Ended"
    else:
        end_text = f"<t:{end_time}:R>\n<t:{end_time}:F>"

    embed.add_field(
        name="⏰ Ends",
        value=end_text,
        inline=True
    )

    embed.add_field(
        name="🏅 Winner Role",
        value="<@&" + str(gw["winner_role"]) + ">",
        inline=True
    )

    embed.add_field(
        name="👥 Participants",
        value=str(len(gw.get("participants", []))),
        inline=True
    )

    embed.add_field(
        name="🍀 Extra Entries",
        value=(
            "2x / 4x / 8x / 16x Luck\n"
            "Regular / Active / Most Active"
        ),
        inline=True
    )

    embed.add_field(
        name="🔒 Requirements",
        value=requirement_text(gw),
        inline=False
    )

    if ended:
        embed.set_footer(text="Giveaway ended")
    else:
        embed.set_footer(text="Good luck everyone! 🎉")

    return embed


# ============================================================
# REQUIREMENT CHECK
# ============================================================

def check_requirements(member, gw):
    req = gw.get("requirements", {})

    if req.get("booster"):
        if not member.premium_since:
            return False, "🚀 You must be a server booster."

    required_role = req.get("role")

    if required_role:
        if not has_role(member, required_role):
            return False, "🔒 You don't have the required role."

    invite_required = int(req.get("invites", 0))

    if invite_required:
        invites = get_valid_invites(member.guild.id, member.id)

        if invites < invite_required:
            return (
                False,
                f"👥 You need **{invite_required} invites**. "
                f"You currently have **{invites}**."
            )

    return True, ""


# ============================================================
# PARTICIPANT PAGINATION
# ============================================================

class ParticipantView(discord.ui.View):

    def __init__(self, participants):
        super().__init__(timeout=120)

        self.participants = participants
        self.page = 0
        self.per_page = 10

    def get_page(self):
        start = self.page * self.per_page
        return self.participants[start:start + self.per_page]

    @discord.ui.button(
        label="◀",
        style=discord.ButtonStyle.secondary
    )
    async def previous(self, interaction, button):

        if self.page > 0:
            self.page -= 1

        await self.update(interaction)

    @discord.ui.button(
        label="▶",
        style=discord.ButtonStyle.secondary
    )
    async def next(self, interaction, button):

        max_page = max(
            0,
            (len(self.participants) - 1) // self.per_page
        )

        if self.page < max_page:
            self.page += 1

        await self.update(interaction)

    async def update(self, interaction):

        people = self.get_page()

        if not people:
            text = "No participants."
        else:
            lines = []

            for i, user_id in enumerate(people, start=1):
                lines.append(f"{i}. <@{user_id}>")

            text = "\n".join(lines)

        embed = discord.Embed(
            title="👥 Giveaway Participants",
            description=text,
            color=discord.Color.white()
        )

        total_pages = max(
            1,
            (len(self.participants) + self.per_page - 1)
            // self.per_page
        )

        embed.set_footer(
            text=f"Page {self.page + 1}/{total_pages}"
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawayView(discord.ui.View):

    def __init__(self, giveaway_id):
        super().__init__(timeout=None)

        self.giveaway_id = str(giveaway_id)

    @discord.ui.button(
        label="🎉 Enter Giveaway",
        style=discord.ButtonStyle.success,
        custom_id="giveaway_enter"
    )
    async def enter(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        gw = get_giveaway(self.giveaway_id)

        if not gw:
            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True
            )
            return

        if gw.get("ended"):
            await interaction.response.send_message(
                "❌ This giveaway has ended.",
                ephemeral=True
            )
            return

        member = interaction.user

        allowed, reason = check_requirements(member, gw)

        if not allowed:
            await interaction.response.send_message(
                reason,
                ephemeral=True
            )
            return

        user_id = str(member.id)

        if user_id in gw["participants"]:
            await interaction.response.send_message(
                "⚠️ You are already entered!",
                ephemeral=True
            )
            return

        gw["participants"].append(user_id)
        save_data()

        try:
            channel = interaction.channel
            message = await channel.fetch_message(gw["message_id"])

            await message.edit(
                embed=giveaway_embed(gw),
                view=GiveawayView(self.giveaway_id)
            )
        except Exception:
            pass

        await interaction.response.send_message(
            "✅ You entered the giveaway!",
            ephemeral=True
        )

    @discord.ui.button(
        label="👥 Participants",
        style=discord.ButtonStyle.secondary,
        custom_id="giveaway_participants"
    )
    async def participants(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        gw = get_giveaway(self.giveaway_id)

        if not gw:
            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True
            )
            return

        participants = gw.get("participants", [])

        view = ParticipantView(participants)

        people = participants[:10]

        if people:
            text = "\n".join(
                f"{i}. <@{uid}>"
                for i, uid in enumerate(people, 1)
            )
        else:
            text = "No participants yet."

        embed = discord.Embed(
            title="👥 Giveaway Participants",
            description=text,
            color=discord.Color.white()
        )

        embed.set_footer(
            text=f"Total participants: {len(participants)}"
        )

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )


# ============================================================
# INVITE REWARD VIEW
# ============================================================

class InviteRewardView(discord.ui.View):

    def __init__(self, reward_id):
        super().__init__(timeout=None)

        self.reward_id = str(reward_id)

    @discord.ui.button(
        label="🎁 Claim Reward",
        style=discord.ButtonStyle.success,
        custom_id="invite_reward_claim"
    )
    async def claim(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        reward = data["invite_rewards"].get(self.reward_id)

        if not reward:
            await interaction.response.send_message(
                "❌ Reward not found.",
                ephemeral=True
            )
            return

        if reward.get("ended"):
            await interaction.response.send_message(
                "❌ This reward has ended.",
                ephemeral=True
            )
            return

        user_id = str(interaction.user.id)

        if user_id in reward.get("claimed", []):
            await interaction.response.send_message(
                "⚠️ You already claimed this reward.",
                ephemeral=True
            )
            return

        invites = get_valid_invites(
            interaction.guild.id,
            interaction.user.id
        )

        required = int(reward["required_invites"])

        if invites < required:
            await interaction.response.send_message(
                f"❌ You need **{required} invites**.\n"
                f"Your invites: **{invites}**",
                ephemeral=True
            )
            return

        reward["claimed"].append(user_id)

        role_id = reward.get("role")

        if role_id:
            role = interaction.guild.get_role(int(role_id))

            if role:
                try:
                    await interaction.user.add_roles(role)
                except Exception:
                    pass

        save_data()

        await interaction.response.send_message(
            f"🎉 **Reward Claimed!**\n\n"
            f"🎁 Reward: **{reward['prize']}**",
            ephemeral=True
        )


# ============================================================
# INVITE TRACKING
# ============================================================

invite_cache = {}


async def cache_invites(guild):
    try:
        invites = await guild.invites()

        invite_cache[guild.id] = {
            invite.code: invite.uses or 0
            for invite in invites
        }

    except Exception:
        invite_cache[guild.id] = {}


@bot.event
async def on_invite_create(invite):
    await cache_invites(invite.guild)


@bot.event
async def on_member_join(member):

    guild = member.guild

    try:
        before = invite_cache.get(guild.id, {})
        invites = await guild.invites()

        used_invite = None

        for invite in invites:
            old_uses = before.get(invite.code, 0)

            if (invite.uses or 0) > old_uses:
                used_invite = invite
                break

        invite_cache[guild.id] = {
            invite.code: invite.uses or 0
            for invite in invites
        }

        if not used_invite:
            return

        inviter = used_invite.inviter

        if not inviter:
            return

        if inviter.bot:
            return

        if inviter.id == member.id:
            return

        guild_data = data["invites"].setdefault(
            str(guild.id),
            {}
        )

        uid = str(inviter.id)

        guild_data[uid] = int(guild_data.get(uid, 0)) + 1

        save_data()

    except Exception:
        pass


@bot.event
async def on_member_remove(member):
    # We intentionally don't automatically remove invites.
    # This prevents easy invite-count abuse through join/leave loops.
    pass


# ============================================================
# PREFIX COMMAND GROUP
# ============================================================

@bot.group(
    name="w",
    invoke_without_command=True
)
async def giveaway_group(ctx):

    if ctx.invoked_subcommand is None:
        await ctx.send(
            f"Use `{PREFIX} help` to see all commands."
        )


# ============================================================
# HELP
# ============================================================

@bot.command(name="help")
async def help_command(ctx):

    embed = discord.Embed(
        title="🤖 SAB Bot Commands",
        color=discord.Color.white()
    )

    embed.add_field(
        name="🎉 Giveaways",
        value=(
            f"`{PREFIX} w create <prize> <winners> <duration>`\n"
            f"`{PREFIX} w end <message_id>`\n"
            f"`{PREFIX} w reroll <message_id>`\n"
            f"`{PREFIX} w claim <message_id>`\n"
            f"`{PREFIX} w participants <message_id>`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎁 Invite Rewards",
        value=(
            f"`{PREFIX} invite reward <invites> <prize>`\n"
            f"`{PREFIX} invite end <id>`\n"
            f"`{PREFIX} invite add <user> <amount>`\n"
            f"`{PREFIX} invite set <user> <amount>`\n"
            f"`{PREFIX} invites <user>`\n"
            f"`{PREFIX} leaderboard`"
        ),
        inline=False
    )

    embed.add_field(
        name="📋 Templates",
        value=(
            f"`{PREFIX} template create <name> <prize> <winners> <duration>`\n"
            f"`{PREFIX} template list`\n"
            f"`{PREFIX} template delete <name>`\n"
            f"`{PREFIX} template start <name>`"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Setup",
        value=(
            f"`{PREFIX} winnerrole <role>`\n"
            f"`{PREFIX} setup`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# ============================================================
# CREATE GIVEAWAY
# ============================================================

@giveaway_group.command(name="create")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def create_giveaway(ctx, prize: str, winners: int, duration: str):

    delta = parse_duration(duration)

    if not delta:
        await ctx.send(
            "❌ Duration example: `10m`, `2h`, `7d`"
        )
        return

    if winners < 1:
        await ctx.send("❌ Winners must be at least 1.")
        return

    settings = data["settings"].setdefault(
        str(ctx.guild.id),
        {}
    )

    winner_role = settings.get("winner_role")

    if not winner_role:
        role = discord.utils.get(
            ctx.guild.roles,
            name="giveaway winner"
        )

        if not role:
            try:
                role = await ctx.guild.create_role(
                    name="giveaway winner"
                )
            except Exception:
                await ctx.send(
                    "❌ Set a winner role first using "
                    f"`{PREFIX} winnerrole @role`"
                )
                return

        winner_role = role.id

    end = now() + delta

    gw_id = str(random.randint(100000000, 999999999))

    gw = {
        "id": gw_id,
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "message_id": 0,
        "prize": prize,
        "winners": winners,
        "end_time": timestamp(end),
        "winner_role": winner_role,
        "participants": [],
        "requirements": {},
        "claim_minutes": 30,
        "ended": False,
        "winner_ids": [],
        "claimed": []
    }

    message = await ctx.send(
        embed=giveaway_embed(gw),
        view=GiveawayView(gw_id)
    )

    gw["message_id"] = message.id

    data["giveaways"][gw_id] = gw

    save_data()

    await message.edit(
        embed=giveaway_embed(gw),
        view=GiveawayView(gw_id)
    )

bot.run(TOKEN)
