import os
import random
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "g.w").strip()

try:
    TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0"))
except:
    TEST_GUILD_ID = 0

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")

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
    command_prefix=PREFIX,
    intents=intents,
    help_command=None
)

# ============================================================
# RUNTIME DATABASE
# ============================================================

giveaways = {}
invite_rewards = {}
templates = {}
invite_counts = {}
guild_settings = {}

# Invite cache
invite_cache = {}

# ============================================================
# HELPERS
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)


def unix_time(dt):
    return int(dt.timestamp())


def parse_duration(text):
    if not text:
        return None

    text = text.lower().strip()

    try:
        amount = int(text[:-1])
        unit = text[-1]

        if amount <= 0:
            return None

        if unit == "s":
            return timedelta(seconds=amount)

        if unit == "m":
            return timedelta(minutes=amount)

        if unit == "h":
            return timedelta(hours=amount)

        if unit == "d":
            return timedelta(days=amount)

    except:
        return None

    return None


def get_invites(guild_id, user_id):
    guild_data = invite_counts.get(guild_id, {})
    return guild_data.get(user_id, 0)


def add_invites(guild_id, user_id, amount):
    guild_data = invite_counts.setdefault(guild_id, {})
    guild_data[user_id] = max(
        0,
        guild_data.get(user_id, 0) + amount
    )


def set_invites(guild_id, user_id, amount):
    guild_data = invite_counts.setdefault(guild_id, {})
    guild_data[user_id] = max(0, amount)


def is_staff(member):
    return (
        member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    )


def get_multiplier(member):

    role_values = {
        "16x Luck": 16,
        "8x Luck": 8,
        "4x Luck": 4,
        "2x Luck": 2
    }

    highest = 1

    for role_name, value in role_values.items():

        role = discord.utils.get(
            member.guild.roles,
            name=role_name
        )

        if role and role in member.roles:
            highest = max(highest, value)

    return highest


def get_extra_entries(member):

    total = get_multiplier(member)

    extras = {
        "Regular": 1,
        "Active": 2,
        "Most Active": 4
    }

    for role_name, value in extras.items():

        role = discord.utils.get(
            member.guild.roles,
            name=role_name
        )

        if role and role in member.roles:
            total += value

    return total


def get_winner_role(guild):

    settings = guild_settings.setdefault(
        guild.id,
        {}
    )

    role_id = settings.get("winner_role")

    if role_id:
        role = guild.get_role(role_id)

        if role:
            return role

    role = discord.utils.get(
        guild.roles,
        name="giveaway winner"
    )

    return role


def giveaway_requirements_text(gw):

    req = gw.get("requirements", {})
    lines = []

    invites = req.get("invites", 0)

    if invites:
        lines.append(
            f"👥 {invites} invites required"
        )

    role_id = req.get("role")

    if role_id:
        lines.append(
            f"🔒 <@&{role_id}> required"
        )

    if req.get("booster"):
        lines.append(
            "🚀 Server Booster required"
        )

    if req.get("bypass_role"):
        lines.append(
            f"🛡️ <@&{req['bypass_role']}> bypass"
        )

    return "\n".join(lines) if lines else "None"


def check_giveaway_requirements(member, gw):

    req = gw.get("requirements", {})

    # Bypass role
    bypass = req.get("bypass_role")

    if bypass:

        role = member.guild.get_role(
            int(bypass)
        )

        if role and role in member.roles:
            return True, ""

    # Booster
    if req.get("booster"):

        if not member.premium_since:
            return False, (
                "🚀 You must be a server booster."
            )

    # Required role
    required_role = req.get("role")

    if required_role:

        role = member.guild.get_role(
            int(required_role)
        )

        if role and role not in member.roles:
            return False, (
                f"🔒 You need {role.mention}."
            )

    # Invites
    required_invites = int(
        req.get("invites", 0)
    )

    if required_invites:

        current = get_invites(
            member.guild.id,
            member.id
        )

        if current < required_invites:

            return False, (
                f"👥 You need **{required_invites} invites**.\n"
                f"You currently have **{current}**."
            )

    return True, ""


# ============================================================
# EMBEDS
# ============================================================

def make_giveaway_embed(gw, ended=False):

    embed = discord.Embed(
        title="🎉 GIVEAWAY",
        color=discord.Color.from_rgb(
            255, 255, 255
        )
    )

    embed.add_field(
        name="🎁 Prize",
        value=f"**{gw['prize']}**",
        inline=False
    )

    embed.add_field(
        name="🏆 Winners",
        value=str(gw["winners"]),
        inline=True
    )

    if ended:
        ends = "Ended"
    else:
        ends = (
            f"<t:{gw['end_time']}:R>\n"
            f"<t:{gw['end_time']}:F>"
        )

    embed.add_field(
        name="⏰ Ends",
        value=ends,
        inline=True
    )

    role_id = gw.get("winner_role")

    embed.add_field(
        name="🏅 Winner Role",
        value=(
            f"<@&{role_id}>"
            if role_id
            else "Not set"
        ),
        inline=True
    )

    embed.add_field(
        name="👥 Participants",
        value=str(
            len(gw.get("participants", []))
        ),
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
        value=giveaway_requirements_text(gw),
        inline=False
    )

    if ended:
        embed.set_footer(
            text="Giveaway ended"
        )
    else:
        embed.set_footer(
            text="Good luck everyone! 🎉"
        )

    return embed


# ============================================================
# PARTICIPANT PAGINATION
# ============================================================

class ParticipantView(discord.ui.View):

    def __init__(self, participants):
        super().__init__(timeout=120)

        self.participants = participants
        self.page = 0
        self.per_page = 10

    def page_users(self):

        start = self.page * self.per_page

        return self.participants[
            start:start + self.per_page
        ]

    async def refresh(self, interaction):

        users = self.page_users()

        if users:

            text = "\n".join(
                f"**{i}.** <@{uid}>"
                for i, uid in enumerate(
                    users,
                    start=self.page * self.per_page + 1
                )
            )

        else:
            text = "No participants."

        total_pages = max(
            1,
            (
                len(self.participants)
                + self.per_page - 1
            ) // self.per_page
        )

        embed = discord.Embed(
            title="👥 Giveaway Participants",
            description=text,
            color=discord.Color.white()
        )

        embed.set_footer(
            text=(
                f"Page {self.page + 1}/{total_pages} • "
                f"Total: {len(self.participants)}"
            )
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self
        )

    @discord.ui.button(
        label="◀",
        style=discord.ButtonStyle.secondary
    )
    async def previous(
        self,
        interaction,
        button
    ):

        if self.page > 0:
            self.page -= 1

        await self.refresh(interaction)

    @discord.ui.button(
        label="▶",
        style=discord.ButtonStyle.secondary
    )
    async def next(
        self,
        interaction,
        button
    ):

        max_page = max(
            0,
            (
                len(self.participants)
                - 1
            ) // self.per_page
        )

        if self.page < max_page:
            self.page += 1

        await self.refresh(interaction)


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawayView(discord.ui.View):

    def __init__(self, giveaway_id):
        super().__init__(timeout=None)

        self.giveaway_id = str(giveaway_id)

        # Unique persistent IDs
        self.enter_button.custom_id = (
            f"gw_enter:{self.giveaway_id}"
        )

        self.participant_button.custom_id = (
            f"gw_participants:{self.giveaway_id}"
        )

    @discord.ui.button(
        label="🎉 Enter Giveaway",
        style=discord.ButtonStyle.success
    )
    async def enter_button(
        self,
        interaction,
        button
    ):

        gw = giveaways.get(
            self.giveaway_id
        )

        if not gw:

            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True
            )
            return

        if gw["ended"]:

            await interaction.response.send_message(
                "❌ This giveaway has ended.",
                ephemeral=True
            )
            return

        allowed, reason = check_giveaway_requirements(
            interaction.user,
            gw
        )

        if not allowed:

            await interaction.response.send_message(
                reason,
                ephemeral=True
            )
            return

        uid = interaction.user.id

        if uid in gw["participants"]:

            await interaction.response.send_message(
                "⚠️ You are already entered!",
                ephemeral=True
            )
            return

        gw["participants"].append(uid)

        try:

            await interaction.message.edit(
                embed=make_giveaway_embed(gw),
                view=GiveawayView(
                    self.giveaway_id
                )
            )

        except:
            pass

        await interaction.response.send_message(
            "✅ You entered the giveaway!",
            ephemeral=True
        )

    @discord.ui.button(
        label="👥 Participants",
        style=discord.ButtonStyle.secondary
    )
    async def participant_button(
        self,
        interaction,
        button
    ):

        gw = giveaways.get(
            self.giveaway_id
        )

        if not gw:

            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True
            )
            return

        users = gw.get(
            "participants",
            []
        )

        page_users = users[:10]

        if page_users:

            text = "\n".join(
                f"**{i}.** <@{uid}>"
                for i, uid in enumerate(
                    page_users,
                    1
                )
            )

        else:
            text = "No participants yet."

        embed = discord.Embed(
            title="👥 Giveaway Participants",
            description=text,
            color=discord.Color.white()
        )

        embed.set_footer(
            text=f"Total participants: {len(users)}"
        )

        await interaction.response.send_message(
            embed=embed,
            view=ParticipantView(users),
            ephemeral=True
        )


# ============================================================
# INVITE REWARD VIEW
# ============================================================

class InviteRewardView(discord.ui.View):

    def __init__(self, reward_id):
        super().__init__(timeout=None)

        self.reward_id = str(reward_id)

        self.claim_button.custom_id = (
            f"invite_claim:{self.reward_id}"
        )

    @discord.ui.button(
        label="🎁 Claim Reward",
        style=discord.ButtonStyle.success
    )
    async def claim_button(
        self,
        interaction,
        button
    ):

        reward = invite_rewards.get(
            self.reward_id
        )

        if not reward:

            await interaction.response.send_message(
                "❌ Reward not found.",
                ephemeral=True
            )
            return

        if reward["ended"]:

            await interaction.response.send_message(
                "❌ This reward has ended.",
                ephemeral=True
            )
            return

        uid = interaction.user.id

        if uid in reward["claimed"]:

            await interaction.response.send_message(
                "⚠️ You already claimed this reward.",
                ephemeral=True
            )
            return

        current = get_invites(
            interaction.guild.id,
            uid
        )

        required = reward["required_invites"]

        if current < required:

            await interaction.response.send_message(
                f"❌ You need **{required} invites**.\n"
                f"Your invites: **{current}**",
                ephemeral=True
            )
            return

        reward["claimed"].append(uid)

        role_id = reward.get("role")

        if role_id:

            role = interaction.guild.get_role(
                int(role_id)
            )

            if role:

                try:
                    await interaction.user.add_roles(
                        role
                    )
                except:
                    pass

        await interaction.response.send_message(
            f"🎉 **Reward Claimed Successfully!**\n\n"
            f"🎁 Reward: **{reward['prize']}**",
            ephemeral=True
        )


# ============================================================
# INVITE TRACKING
# ============================================================

async def update_invite_cache(guild):

    try:

        invites = await guild.invites()

        invite_cache[guild.id] = {
            invite.code: invite.uses or 0
            for invite in invites
        }

    except:
        invite_cache[guild.id] = {}


@bot.event
async def on_invite_create(invite):

    await update_invite_cache(
        invite.guild
    )


@bot.event
async def on_member_join(member):

    guild = member.guild

    try:

        old_cache = invite_cache.get(
            guild.id,
            {}
        )

        invites = await guild.invites()

        used = None

        for invite in invites:

            old_uses = old_cache.get(
                invite.code,
                0
            )

            new_uses = invite.uses or 0

            if new_uses > old_uses:

                used = invite
                break

        invite_cache[guild.id] = {
            invite.code: invite.uses or 0
            for invite in invites
        }

        if not used:
            return

        inviter = used.inviter

        if not inviter:
            return

        if inviter.bot:
            return

        if inviter.id == member.id:
            return

        add_invites(
            guild.id,
            inviter.id,
            1
        )

        print(
            f"Invite: {inviter} invited {member}"
        )

    except Exception as e:

        print(
            f"Invite tracking error: {e}"
        )


# ============================================================
# HELP
# ============================================================

@bot.command(name="help")
async def help_command(ctx):

    embed = discord.Embed(
        title="🤖 SAB BOT COMMANDS",
        color=discord.Color.white()
    )

    embed.add_field(
        name="🎉 Giveaways",
        value=(
            f"`{PREFIX} create <prize> <winners> <duration>`\n"
            f"`{PREFIX} end <message_id>`\n"
            f"`{PREFIX} reroll <message_id>`\n"
            f"`{PREFIX} claim <message_id>`\n"
            f"`{PREFIX} participants <message_id>`\n"
            f"`{PREFIX} requirement <message_id> ...`\n"
            f"`{PREFIX} claimtime <message_id> <minutes>`"
        ),
        inline=False
    )

    embed.add_field(
        name="🎁 Invite Rewards",
        value=(
            f"`{PREFIX} invite reward <invites> <prize>`\n"
            f"`{PREFIX} invite end <id>`\n"
            f"`{PREFIX} invite add @user <amount>`\n"
            f"`{PREFIX} invite set @user <amount>`\n"
            f"`{PREFIX} invites [@user]`\n"
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
            f"`{PREFIX} setup`\n"
            f"`{PREFIX} winnerrole @role`"
        ),
        inline=False
    )

    await ctx.send(embed=embed)


# ============================================================
# CREATE GIVEAWAY
# ============================================================

@bot.command(name="create")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def create_giveaway(
    ctx,
    winners: int,
    duration: str,
    *,
    prize: str
):

    if winners < 1:

        await ctx.send(
            "❌ Winners must be at least 1."
        )
        return

    delta = parse_duration(duration)

    if not delta:

        await ctx.send(
            "❌ Duration examples: `10m`, `2h`, `7d`"
        )
        return

    role = get_winner_role(
        ctx.guild
    )

    if not role:

        try:

            role = await ctx.guild.create_role(
                name="giveaway winner"
            )

        except:

            await ctx.send(
                "❌ I couldn't create the winner role."
            )
            return

    giveaway_id = str(
        random.randint(
            100000000,
            999999999
        )
    )

    end = utcnow() + delta

    gw = {

        "id": giveaway_id,

        "guild_id": ctx.guild.id,

        "channel_id": ctx.channel.id,

        "message_id": 0,

        "prize": prize,

        "winners": winners,

        "end_time": unix_time(end),

        "winner_role": role.id,

        "participants": [],

        "requirements": {
            "invites": 0,
            "role": None,
            "booster": False,
            "bypass_role": None
        },

        "claim
