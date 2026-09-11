import os

files = {
    "requirements.txt": """discord.py>=2.3.2
aiosqlite>=0.19.0
python-dotenv>=1.0.0
""",

    ".env.example": """DISCORD_TOKEN=your_bot_token_here
PREFIX=g.w
TEST_GUILD_ID=123456789012345678
""",

    "bot.py": '''import os
import sys
import asyncio
import random
import datetime
import math
from typing import Optional, List, Dict, Any, Union

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiosqlite

# Load Environment Variables
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_PREFIX = os.getenv("PREFIX", "g.w")
TEST_GUILD_ID = os.getenv("TEST_GUILD_ID")
DB_FILE = "bot_database.db"

if not DISCORD_TOKEN:
    print("CRITICAL ERROR: DISCORD_TOKEN is missing in environment variables.")
    sys.exit(1)

# --- DATABASE SETUP ---

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                prefix TEXT,
                manager_role_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id TEXT PRIMARY KEY,
                guild_id INTEGER,
                channel_id INTEGER,
                message_id INTEGER,
                prize TEXT,
                winners_count INTEGER,
                start_timestamp REAL,
                end_timestamp REAL,
                claim_time_seconds INTEGER,
                winner_role_id INTEGER,
                min_invites INTEGER,
                required_role_id INTEGER,
                bypass_role_id INTEGER,
                req_booster INTEGER,
                status TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                giveaway_id TEXT,
                user_id INTEGER,
                entries INTEGER,
                joined_at REAL,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_winners (
                giveaway_id TEXT,
                user_id INTEGER,
                status TEXT,
                claim_deadline REAL,
                drawn_at REAL,
                claimed_by INTEGER,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS extra_roles (
                guild_id INTEGER,
                role_id INTEGER,
                multiplier INTEGER,
                PRIMARY KEY (guild_id, role_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                guild_id INTEGER,
                name TEXT,
                prize TEXT,
                winners INTEGER,
                duration TEXT,
                claim_time TEXT,
                PRIMARY KEY (guild_id, name)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                guild_id INTEGER,
                user_id INTEGER,
                invites INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_rewards (
                id TEXT PRIMARY KEY,
                guild_id INTEGER,
                name TEXT,
                req_invites INTEGER,
                role_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS invite_claims (
                reward_id TEXT,
                guild_id INTEGER,
                user_id INTEGER,
                claimed_at REAL,
                PRIMARY KEY (reward_id, user_id)
            )
        """)
        await db.commit()

def parse_duration(time_str: str) -> Optional[int]:
    time_str = time_str.lower().strip()
    seconds = 0
    current_num = ""
    for char in time_str:
        if char.isdigit():
            current_num += char
        else:
            if not current_num:
                return None
            num = int(current_num)
            current_num = ""
            if char == 's': seconds += num
            elif char == 'm': seconds += num * 60
            elif char == 'h': seconds += num * 3600
            elif char == 'd': seconds += num * 86400
            else: return None
    if current_num:
        seconds += int(current_num)
    return seconds if seconds > 0 else None

# --- BOT CLIENT ---

class SABBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.invites = True
        intents.guilds = True

        async def get_prefix(bot_instance, message):
            if not message.guild:
                return DEFAULT_PREFIX
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT prefix FROM settings WHERE guild_id = ?", (message.guild.id,)) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0]:
                        return row[0]
            return DEFAULT_PREFIX

        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True
        )
        self.invite_cache: Dict[int, Dict[str, int]] = {}

    async def setup_hook(self):
        print("[INIT] Initializing SQLite Database...")
        await init_db()

        print("[INIT] Restoring Persistent UI Views...")
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute("SELECT id FROM giveaways WHERE status = 'active'") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    self.add_view(PersistentGiveawayView(row[0]))

            async with db.execute("SELECT id FROM invite_rewards") as cursor:
                rows = await cursor.fetchall()
                for row in rows:
                    self.add_view(PersistentRewardView(row[0]))

        check_ended_giveaways.start(self)
        check_expired_claim_timers.start(self)

        if TEST_GUILD_ID:
            guild = discord.Object(id=int(TEST_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"[SYNC] Synced to guild: {TEST_GUILD_ID}")
        else:
            await self.tree.sync()
            print("[SYNC] Synced globally.")

bot = SABBot()

# --- PERMISSIONS ---

async def is_staff_or_admin(ctx_or_interaction: Union[commands.Context, discord.Interaction]) -> bool:
    user = ctx_or_interaction.author if isinstance(ctx_or_interaction, commands.Context) else ctx_or_interaction.user
    guild = ctx_or_interaction.guild
    if not guild or not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator or user.guild_permissions.manage_guild:
        return True
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT manager_role_id FROM settings WHERE guild_id = ?", (guild.id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                role = guild.get_role(row[0])
                if role and role in user.roles:
                    return True
    return False

async def clean_cmd_msg(ctx: commands.Context):
    try:
        if ctx.guild and ctx.message:
            await ctx.message.delete()
    except Exception:
        pass

# --- UI VIEWS ---

class PersistentGiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

        btn_enter = discord.ui.Button(label="Enter Giveaway", emoji="🎉", style=discord.ButtonStyle.primary, custom_id=f"gw_enter:{giveaway_id}")
        btn_enter.callback = self.enter_callback
        self.add_item(btn_enter)

        btn_participants = discord.ui.Button(label="Participants", emoji="👥", style=discord.ButtonStyle.secondary, custom_id=f"gw_participants:{giveaway_id}")
        btn_participants.callback = self.participants_callback
        self.add_item(btn_participants)

    async def enter_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM giveaways WHERE id = ?", (self.giveaway_id,)) as cursor:
                gw = await cursor.fetchone()

            if not gw or gw["status"] != "active":
                return await interaction.followup.send("❌ Giveaway is inactive.", ephemeral=True)

            user = interaction.user
            guild = interaction.guild

            bypass_role_id = gw["bypass_role_id"]
            has_bypass = False
            if bypass_role_id:
                b_role = guild.get_role(bypass_role_id)
                if b_role and b_role in user.roles:
                    has_bypass = True

            if not has_bypass:
                req_role_id = gw["required_role_id"]
                if req_role_id:
                    r_role = guild.get_role(req_role_id)
                    if r_role and r_role not in user.roles:
                        return await interaction.followup.send(f"❌ Requires {r_role.mention} role.", ephemeral=True)

                if gw["req_booster"] and not user.premium_since:
                    return await interaction.followup.send("❌ Must be a Server Booster.", ephemeral=True)

                min_invites = gw["min_invites"] or 0
                if min_invites > 0:
                    async with db.execute("SELECT invites FROM invites WHERE guild_id = ? AND user_id = ?", (guild.id, user.id)) as icursor:
                        irow = await icursor.fetchone()
                        user_invs = irow[0] if irow else 0
                    if user_invs < min_invites:
                        return await interaction.followup.send(f"❌ Requires **{min_invites}** invites. You have **{user_invs}**.", ephemeral=True)

            async with db.execute("SELECT 1 FROM giveaway_participants WHERE giveaway_id = ? AND user_id = ?", (self.giveaway_id, user.id)) as pcursor:
                if await pcursor.fetchone():
                    return await interaction.followup.send("⚠️ You already entered!", ephemeral=True)

            entries = 1
            async with db.execute("SELECT role_id, multiplier FROM extra_roles WHERE guild_id = ?", (guild.id,)) as ecursor:
                async for er in ecursor:
                    role = guild.get_role(er[0])
                    if role and role in user.roles:
                        entries = max(entries, er[1])

            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            await db.execute(
                "INSERT INTO giveaway_participants (giveaway_id, user_id, entries, joined_at) VALUES (?, ?, ?, ?)",
                (self.giveaway_id, user.id, entries, now)
            )
            await db.commit()

            async with db.execute("SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id = ?", (self.giveaway_id,)) as count_cursor:
                p_count = (await count_cursor.fetchone())[0]

            channel = guild.get_channel(gw["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(gw["message_id"])
                    if msg and msg.embeds:
                        embed = msg.embeds[0]
                        for i, field in enumerate(embed.fields):
                            if field.name == "👥 Participants":
                                embed.set_field_at(i, name="👥 Participants", value=str(p_count), inline=True)
                                break
                        await msg.edit(embed=embed)
                except Exception:
                    pass

            await interaction.followup.send(f"🎉 **Entered!** multiplier: **{entries}x**", ephemeral=True)

    async def participants_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT user_id, entries FROM giveaway_participants WHERE giveaway_id = ?", (self.giveaway_id,)) as cursor:
                participants = await cursor.fetchall()

        if not participants:
            return await interaction.followup.send("👥 No participants yet.", ephemeral=True)
        paginator = ParticipantPaginator(interaction, participants)
        await paginator.send_initial()

class ParticipantPaginator(discord.ui.View):
    def __init__(self, interaction: Union[discord.Interaction, commands.Context], participants: list):
        super().__init__(timeout=120)
        self.interaction = interaction
        self.participants = participants
        self.page_size = 10
        self.current_page = 0
        self.max_pages = math.ceil(len(participants) / self.page_size)

        self.btn_prev = discord.ui.Button(label="Previous", emoji="⬅️", style=discord.ButtonStyle.secondary)
        self.btn_next = discord.ui.Button(label="Next", emoji="➡️", style=discord.ButtonStyle.secondary)
        self.btn_close = discord.ui.Button(label="Close", emoji="❌", style=discord.ButtonStyle.danger)

        self.btn_prev.callback = self.prev_page
        self.btn_next.callback = self.next_page
        self.btn_close.callback = self.close_view

        self.add_item(self.btn_prev)
        self.add_item(self.btn_next)
        self.add_item(self.btn_close)
        self.update_buttons()

    def update_buttons(self):
        self.btn_prev.disabled = self.current_page == 0
        self.btn_next.disabled = self.current_page >= self.max_pages - 1

    def build_embed(self):
        start = self.current_page * self.page_size
        end = start + self.page_size
        slice_parts = self.participants[start:end]
        lines = [f"**{idx}.** <@{p['user_id']}> ({p['entries']} entries)" for idx, p in enumerate(slice_parts, start=start + 1)]
        embed = discord.Embed(title=f"👥 Participants ({self.current_page + 1}/{self.max_pages})", description="\n".join(lines), color=0xFFFFFF)
        embed.set_footer(text=f"Total: {len(self.participants)}")
        return embed

    async def send_initial(self):
        if isinstance(self.interaction, discord.Interaction):
            await self.interaction.followup.send(embed=self.build_embed(), view=self, ephemeral=True)

    async def prev_page(self, interaction: discord.Interaction):
        if self.current_page > 0:
            self.current_page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def next_page(self, interaction: discord.Interaction):
        if self.current_page < self.max_pages - 1:
            self.current_page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def close_view(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Closed.", embed=None, view=None)

class PersistentRewardView(discord.ui.View):
    def __init__(self, reward_id: str):
        super().__init__(timeout=None)
        self.reward_id = reward_id

        btn_claim = discord.ui.Button(label="Claim Reward", emoji="🎁", style=discord.ButtonStyle.success, custom_id=f"invite_reward_claim:{reward_id}")
        btn_claim.callback = self.claim_callback
        self.add_item(btn_claim)

    async def claim_callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        guild = interaction.guild

        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM invite_rewards WHERE id = ?", (self.reward_id,)) as cursor:
                reward = await cursor.fetchone()

            if not reward:
                return await interaction.followup.send("❌ Reward deleted.", ephemeral=True)

            async with db.execute("SELECT 1 FROM invite_claims WHERE reward_id = ? AND user_id = ?", (self.reward_id, user.id)) as ccursor:
                if await ccursor.fetchone():
                    return await interaction.followup.send("⚠️ Reward already claimed!", ephemeral=True)

            async with db.execute("SELECT invites FROM invites WHERE guild_id = ? AND user_id = ?", (guild.id, user.id)) as icursor:
                irow = await icursor.fetchone()
                current_invites = irow["invites"] if irow else 0

            if current_invites < reward["req_invites"]:
                return await interaction.followup.send(f"❌ Needs **{reward['req_invites']}** invites. You have **{current_invites}**.", ephemeral=True)

            if reward["role_id"]:
                role = guild.get_role(reward["role_id"])
                if role:
                    try: await user.add_roles(role, reason="Invite Reward Claimed")
                    except discord.Forbidden: return await interaction.followup.send("❌ Missing permissions to assign role.", ephemeral=True)

            now = datetime.datetime.now(datetime.timezone.utc).timestamp()
            await db.execute("INSERT INTO invite_claims (reward_id, guild_id, user_id, claimed_at) VALUES (?, ?, ?, ?)", (self.reward_id, guild.id, user.id, now))
            await db.commit()

        await interaction.followup.send(f"🎉 **Claimed {reward['name']}!**", ephemeral=True)

# --- GIVEAWAY ENGINE ---

async def draw_giveaway_winners(giveaway_id: str, count: int, exclude_user_ids: list = None) -> list:
    if exclude_user_ids is None: exclude_user_ids = []
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id, entries FROM giveaway_participants WHERE giveaway_id = ?", (giveaway_id,)) as cursor:
            all_participants = await cursor.fetchall()

    pool = []
    for p in all_participants:
        if p["user_id"] not in exclude_user_ids:
            pool.extend([p["user_id"]] * p["entries"])
    if not pool: return []
    winners = []
    for _ in range(count):
        if not pool: break
        chosen = random.choice(pool)
        winners.append(chosen)
        pool = [uid for uid in pool if uid != chosen]
    return winners

async def process_end_giveaway(gw: dict):
    giveaway_id = gw["id"]
    guild = bot.get_guild(gw["guild_id"])
    if not guild:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute("UPDATE giveaways SET status = 'ended' WHERE id = ?", (giveaway_id,))
            await db.commit()
        return

    channel = guild.get_channel(gw["channel_id"])
    winner_ids = await draw_giveaway_winners(giveaway_id, gw["winners_count"])
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    claim_deadline = now + gw["claim_time_seconds"]

    winner_mentions = []
    async with aiosqlite.connect(DB_FILE) as db:
        for uid in winner_ids:
            member = guild.get_member(uid)
            if member:
                winner_mentions.append(member.mention)
                if gw["winner_role_id"]:
                    w_role = guild.get_role(gw["winner_role_id"])
                    if w_role:
                        try: await member.add_roles(w_role, reason="Giveaway Winner")
                        except Exception: pass

            await db.execute("""
                INSERT INTO giveaway_winners (giveaway_id, user_id, status, claim_deadline, drawn_at)
                VALUES (?, ?, 'pending', ?, ?)
                
