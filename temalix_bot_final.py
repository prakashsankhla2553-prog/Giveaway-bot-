
import os
import json
import random
import asyncio
import time
from pathlib import Path

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ============================================================
# TEMALIX-SAFE VERSION
# No pymongo, no requests, no external database package.
# Data is stored in bot_data.json beside this file.
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN")
PREFIX = os.getenv("PREFIX", "g.w")
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0") or 0)

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing from Temalix Environment Variables.")

DATA_FILE = Path("bot_data.json")
DATA = {
    "settings": {},
    "giveaways": {},
    "participants": {},
    "winners": {},
    "extra_roles": {},
    "templates": {},
    "invites": {},
    "invite_rewards": {},
    "invite_reward_claims": {},
}

def load_data():
    global DATA
    try:
        if DATA_FILE.exists():
            with DATA_FILE.open("r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in DATA:
                if isinstance(saved.get(k), dict):
                    DATA[k] = saved[k]
    except Exception as e:
        print("Data load warning:", repr(e))

def save_data():
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(DATA, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)

load_data()

def now():
    return int(time.time())

def new_id():
    import uuid
    return uuid.uuid4().hex

def parse_duration(value):
    value = value.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if not value[:-1].isdigit() or value[-1] not in units:
        raise ValueError("Use `30m`, `2h`, `3d`, or `1w`.")
    return int(value[:-1]) * units[value[-1]]

def get_role(guild, role_id):
    try:
        return guild.get_role(int(role_id)) if role_id else None
    except Exception:
        return None

def role_mention(guild, role_id):
    role = get_role(guild, role_id)
    return role.mention if role else "None"

def is_staff(member):
    if member.guild_permissions.administrator:
        return True
    setting = DATA["settings"].get(str(member.guild.id), {})
    role_id = setting.get("manager_role_id")
    return bool(role_id and any(r.id == int(role_id) for r in member.roles))

def invite_count(guild_id, user_id):
    return int(DATA["invites"].get(f"{guild_id}:{user_id}", 0))

def extra_entries(member):
    total = 0
    guild_roles = DATA["extra_roles"].get(str(member.guild.id), {})
    member_role_ids = {r.id for r in member.roles}
    for role_id, amount in guild_roles.items():
        if int(role_id) in member_role_ids:
            total += int(amount)
    return total

def requirements_ok(g, member):
    bypass = g.get("bypass_role_id")
    if bypass and any(r.id == int(bypass) for r in member.roles):
        return True
    required = g.get("required_role_id")
    if required and not any(r.id == int(required) for r in member.roles):
        return False
    if invite_count(member.guild.id, member.id) < int(g.get("min_invites", 0)):
        return False
    if g.get("booster_required") and not member.premium_since:
        return False
    return True

def participants(gid):
    return DATA["participants"].get(gid, [])

def add_participant(gid, uid):
    DATA["participants"].setdefault(gid, [])
    if uid not in DATA["participants"][gid]:
        DATA["participants"][gid].append(uid)
        save_data()

def choose_winners(guild, g, count, exclude=None):
    exclude = set(exclude or [])
    pool = []
    for uid in participants(g["_id"]):
        if int(uid) in exclude:
            continue
        member = guild.get_member(int(uid))
        if member and requirements_ok(g, member):
            pool.append((member, 1 + extra_entries(member)))
    chosen = []
    while pool and len(chosen) < count:
        total = sum(w for _, w in pool)
        pick = random.uniform(0, total)
        cur = 0
        index = 0
        for i, (_, weight) in enumerate(pool):
            cur += weight
            if pick <= cur:
                index = i
                break
        chosen.append(pool.pop(index)[0])
    return chosen

def giveaway_embed(g, guild):
    status = g.get("status", "active")
    count = len(participants(g["_id"]))
    e = discord.Embed(
        title="🎉 GIVEAWAY",
        color=discord.Color.from_rgb(255, 255, 255)
    )
    e.add_field(name="🎁 Prize", value=str(g["prize"]), inline=True)
    e.add_field(name="🏆 Winners", value=str(g["winner_count"]), inline=True)
    e.add_field(
        name="⏰ Ends",
        value=f"<t:{int(g['ends_at'])}:F>\n<t:{int(g['ends_at'])}:R>",
        inline=True
    )
    e.add_field(
        name="👑 Winner Role",
        value=role_mention(guild, g.get("winner_role_id")),
        inline=True
    )
    e.add_field(name="👥 Participants", value=str(count), inline=True)

    extras = DATA["extra_roles"].get(str(guild.id), {})
    extra_lines = []
    for rid, amount in extras.items():
        role = get_role(guild, rid)
        if role:
            extra_lines.append(f"{role.mention} = +{amount}")
    e.add_field(
        name="🍀 Extra Entries",
        value="\n".join(extra_lines) if extra_lines else "None",
        inline=True
    )

    req = []
    if int(g.get("min_invites", 0)):
        req.append(f"• {g['min_invites']} valid invite(s)")
    if g.get("required_role_id"):
        req.append(f"• Role: {role_mention(guild, g['required_role_id'])}")
    if g.get("booster_required"):
        req.append("• Server Booster")
    if g.get("bypass_role_id"):
        req.append(f"• Bypass: {role_mention(guild, g['bypass_role_id'])}")

    e.add_field(
        name="📋 Requirements",
        value="\n".join(req) if req else "• None",
        inline=False
    )

    if status == "active":
        e.set_footer(text="Click 🎉 Enter Giveaway to join")
    elif status == "claiming":
        e.set_footer(text="Ended • Winners must be verified by staff")
    else:
        e.set_footer(text="Giveaway completed")
    return e

class ParticipantsView(discord.ui.View):
    def __init__(self, gid, page=0):
        super().__init__(timeout=120)
        self.gid = gid
        self.page = page
        total = len(participants(gid))
        self.pages = max(1, (total + 9) // 10)

        back = discord.ui.Button(label="Previous", emoji="◀️")
        nxt = discord.ui.Button(label="Next", emoji="▶️")
        back.disabled = page <= 0
        nxt.disabled = page >= self.pages - 1
        back.callback = self.previous
        nxt.callback = self.next
        self.add_item(back)
        self.add_item(nxt)

    async def previous(self, interaction):
        await show_participants(interaction, self.gid, max(0, self.page - 1))

    async def next(self, interaction):
        await show_participants(interaction, self.gid, min(self.pages - 1, self.page + 1))

async def show_participants(interaction, gid, page):
    ids = participants(gid)
    pages = max(1, (len(ids) + 9) // 10)
    page = max(0, min(page, pages - 1))
    chunk = ids[page * 10:(page + 1) * 10]
    text = "\n".join(
        f"`{page * 10 + i}.` <@{uid}>"
        for i, uid in enumerate(chunk, 1)
    ) or "No participants yet."
    e = discord.Embed(
        title="👥 Giveaway Participants",
        description=text,
        color=discord.Color.blurple()
    )
    e.set_footer(text=f"Page {page + 1}/{pages} • {len(ids)} total")
    await interaction.response.send_message(
        embed=e,
        view=ParticipantsView(gid, page),
        ephemeral=True
    )

class GiveawayView(discord.ui.View):
    def __init__(self, gid):
        super().__init__(timeout=None)
        self.gid = gid

        enter = discord.ui.Button(
            label="Enter Giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=f"gw_enter:{gid}"
        )
        people = discord.ui.Button(
            label="Participants",
            emoji="👥",
            style=discord.ButtonStyle.secondary,
            custom_id=f"gw_people:{gid}"
        )
        enter.callback = self.enter
        people.callback = self.people
        self.add_item(enter)
        self.add_item(people)

    async def enter(self, interaction):
        g = DATA["giveaways"].get(self.gid)
        if not g or g.get("status") != "active":
            return await interaction.response.send_message(
                "❌ This giveaway is no longer active.", ephemeral=True
            )

        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return await interaction.response.send_message(
                "❌ Member data unavailable.", ephemeral=True
            )

        if not requirements_ok(g, member):
            return await interaction.response.send_message(
                "❌ You don't meet the giveaway requirements.", ephemeral=True
            )

        if interaction.user.id in participants(self.gid):
            return await interaction.response.send_message(
                "✅ You are already entered!", ephemeral=True
            )

        add_participant(self.gid, interaction.user.id)
        await interaction.response.send_message(
            "🎉 You entered the giveaway!", ephemeral=True
        )
        await refresh_giveaway(g)

    async def people(self, interaction):
        await show_participants(interaction, self.gid, 0)

class InviteRewardView(discord.ui.View):
    def __init__(self, rid):
        super().__init__(timeout=None)
        self.rid = rid
        b = discord.ui.Button(
            label="Claim Reward",
            emoji="🎁",
            style=discord.ButtonStyle.success,
            custom_id=f"ir_claim:{rid}"
        )
        b.callback = self.claim
        self.add_item(b)

    async def claim(self, interaction):
        r = DATA["invite_rewards"].get(self.rid)
        if not r or r.get("status") != "active" or now() >= int(r["ends_at"]):
            return await interaction.response.send_message(
                "❌ This invite reward has expired.", ephemeral=True
            )

        current = invite_count(interaction.guild.id, interaction.user.id)
        if current < int(r["required_invites"]):
            return await interaction.response.send_message(
                f"❌ You need **{r['required_invites']}** invites. You have **{current}**.",
                ephemeral=True
            )

        key = f"{self.rid}:{interaction.user.id}"
        if key in DATA["invite_reward_claims"]:
            return await interaction.response.send_message(
                "❌ You already claimed this reward.", ephemeral=True
            )

        DATA["invite_reward_claims"][key] = now()
        role = get_role(interaction.guild, r.get("reward_role_id"))
        if role:
            try:
                await interaction.user.add_roles(role, reason="Invite reward claimed")
            except discord.Forbidden:
                pass
        save_data()

        await interaction.response.send_message(
            f"🎁 **Reward claimed!** You received **{r['prize']}**.",
            ephemeral=True
        )

async def refresh_giveaway(g):
    guild = bot.get_guild(int(g["guild_id"]))
    if not guild:
        return
    channel = guild.get_channel(int(g["channel_id"]))
    if not channel or not g.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(int(g["message_id"]))
        await msg.edit(embed=giveaway_embed(g, guild), view=GiveawayView(g["_id"]))
    except Exception as e:
        print("Giveaway refresh warning:", repr(e))

async def add_role(member, role):
    if role:
        try:
            await member.add_roles(role, reason="Giveaway winner")
        except discord.Forbidden:
            print("Cannot add winner role. Put bot role above the winner role.")

async def remove_role(member, role):
    if role:
        try:
            await member.remove_roles(role, reason="Giveaway claim completed/expired")
        except discord.Forbidden:
            pass

async def finish_giveaway(g):
    if g.get("status") != "active":
        return
    guild = bot.get_guild(int(g["guild_id"]))
    if not guild:
        return

    already = {
        int(w["user_id"])
        for w in DATA["winners"].values()
        if w.get("giveaway_id") == g["_id"]
    }
    winners = choose_winners(guild, g, int(g["winner_count"]), already)

    if not winners:
        g["status"] = "completed"
        g["ended_at"] = now()
        DATA["giveaways"][g["_id"]] = g
        save_data()
        await refresh_giveaway(g)
        return

    deadline = now() + int(g.get("claim_seconds", 1800))
    role = get_role(guild, g.get("winner_role_id"))

    for member in winners:
        wid = new_id()
        DATA["winners"][wid] = {
            "_id": wid,
            "giveaway_id": g["_id"],
            "guild_id": guild.id,
            "user_id": member.id,
            "status": "pending",
            "deadline_at": deadline,
            "created_at": now()
        }
        await add_role(member, role)

    g["status"] = "claiming"
    g["ended_at"] = now()
    DATA["giveaways"][g["_id"]] = g
    save_data()
    await refresh_giveaway(g)

    channel = guild.get_channel(int(g["channel_id"]))
    if channel:
        mentions = " ".join(f"<@{m.id}>" for m in winners)
        mins = max(1, int(g.get("claim_seconds", 1800)) // 60)
        await channel.send(
            f"🏆 **Giveaway Ended!**\n"
            f"🎁 Prize: **{g['prize']}**\n"
            f"👑 Winner(s): {mentions}\n"
            f"⏳ Winners have **{mins} minutes** to be verified.\n"
            f"Staff: verify the winner in a ticket, then use "
            f"`{PREFIX} claim {g['message_id']}`."
        )

async def expire_winner(wid, w):
    g = DATA["giveaways"].get(w["giveaway_id"])
    if not g or g.get("status") != "claiming":
        return
    guild = bot.get_guild(int(w["guild_id"]))
    if not guild:
        return

    member = guild.get_member(int(w["user_id"]))
    role = get_role(guild, g.get("winner_role_id"))
    if member:
        await remove_role(member, role)

    w["status"] = "expired"
    w["expired_at"] = now()
    DATA["winners"][wid] = w

    used = {
        int(x["user_id"])
        for x in DATA["winners"].values()
        if x.get("giveaway_id") == g["_id"]
    }
    replacement = choose_winners(guild, g, 1, used)

    if replacement:
        m = replacement[0]
        new_wid = new_id()
        DATA["winners"][new_wid] = {
            "_id": new_wid,
            "giveaway_id": g["_id"],
            "guild_id": guild.id,
            "user_id": m.id,
            "status": "pending",
            "deadline_at": now() + int(g.get("claim_seconds", 1800)),
            "created_at": now()
        }
        await add_role(m, role)
        channel = guild.get_channel(int(g["channel_id"]))
        if channel:
            await channel.send(
                f"🔄 **Giveaway Reroll!**\n"
                f"<@{w['user_id']}> did not claim in time.\n"
                f"🏆 New winner: {m.mention}"
            )
    else:
        # No replacement available; the slot is simply closed.
        pass
    save_data()

async def timer_loop():
    while True:
        try:
            for gid, g in list(DATA["giveaways"].items()):
                if g.get("status") == "active" and int(g.get("ends_at", 0)) <= now():
                    await finish_giveaway(g)

            for wid, w in list(DATA["winners"].items()):
                if w.get("status") == "pending" and int(w.get("deadline_at", 0)) <= now():
                    await expire_winner(wid, w)

            for rid, r in list(DATA["invite_rewards"].items()):
                if r.get("status") == "active" and int(r.get("ends_at", 0)) <= now():
                    r["status"] = "expired"
                    DATA["invite_rewards"][rid] = r
                    save_data()
        except Exception as e:
            print("Timer error:", repr(e))
        await asyncio.sleep(15)

# -------------------- Invite tracking --------------------

invite_cache = {}

async def cache_invites():
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {
                i.code: (i.uses or 0, i.inviter.id if i.inviter else 0)
                for i in invites
            }
        except Exception as e:
            print(f"Invite cache failed for {guild.name}: {e}")

@bot.event
async def on_member_join(member):
    if member.bot:
        return
    try:
        before = invite_cache.get(member.guild.id, {})
        invites = await member.guild.invites()
        after = {
            i.code: (i.uses or 0, i.inviter.id if i.inviter else 0)
            for i in invites
        }
        invite_cache[member.guild.id] = after

        for code, (uses, inviter_id) in after.items():
            old_uses = before.get(code, (0, inviter_id))[0]
            if uses > old_uses and inviter_id and inviter_id != member.id:
                key = f"{member.guild.id}:{inviter_id}"
                DATA["invites"][key] = invite_count(member.guild.id, inviter_id) + 1
                save_data()
                break
    except Exception as e:
        print("Invite tracking error:", repr(e))

# -------------------- Bot --------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
slash_synced = False
started_views = False

@bot.event
async def on_ready():
    global slash_synced, started_views

    print(f"ONLINE: {bot.user} ({bot.user.id})")

    if not started_views:
        for gid, g in DATA["giveaways"].items():
            if g.get("status") in ("active", "claiming") and g.get("message_id"):
                bot.add_view(GiveawayView(gid))
        for rid, r in DATA["invite_rewards"].items():
            if r.get("status") == "active":
                bot.add_view(InviteRewardView(rid))
        started_views = True

    await cache_invites()

    if not slash_synced:
        try:
            if TEST_GUILD_ID:
                guild_obj = discord.Object(id=TEST_GUILD_ID)
                bot.tree.copy_global_to(guild=guild_obj)
                await bot.tree.sync(guild=guild_obj)
                print("SLASH COMMANDS: synced to test server")
            else:
                await bot.tree.sync()
                print("SLASH COMMANDS: synced globally")
            slash_synced = True
        except Exception as e:
            print("SLASH SYNC ERROR:", repr(e))

    if not timer_loop_task.is_running():
        timer_loop_task.start()

timer_loop_task = tasks.loop(seconds=15)(timer_loop)

# -------------------- Prefix commands --------------------

@bot.command(name="claim")
@commands.guild_only()
async def claim(ctx, message_id: int, member: discord.Member = None):
    if not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass

    g = next(
        (x for x in DATA["giveaways"].values()
         if int(x.get("guild_id")) == ctx.guild.id and int(x.get("message_id", 0)) == message_id),
        None
    )
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)

    pending = [
        (wid, w) for wid, w in DATA["winners"].items()
        if w.get("giveaway_id") == g["_id"] and w.get("status") == "pending"
    ]

    if member:
        pending = [(wid, w) for wid, w in pending if int(w["user_id"]) == member.id]

    if not pending:
        return await ctx.send("❌ No pending winner found.", delete_after=8)

    wid, w = pending[0]
    w["status"] = "claimed"
    w["claimed_at"] = now()
    w["claimed_by"] = ctx.author.id
    DATA["winners"][wid] = w

    winner = ctx.guild.get_member(int(w["user_id"]))
    role = get_role(ctx.guild, g.get("winner_role_id"))
    if winner:
        await remove_role(winner, role)

    if not any(
        x.get("giveaway_id") == g["_id"] and x.get("status") == "pending"
        for x in DATA["winners"].values()
    ):
        g["status"] = "completed"
        DATA["giveaways"][g["_id"]] = g

    save_data()

    e = discord.Embed(
        title="🏆 Giveaway Claimed Successfully!",
        color=discord.Color.green()
    )
    e.add_field(name="🎁 Prize", value=g["prize"], inline=True)
    e.add_field(name="👑 Winner", value=f"<@{w['user_id']}>", inline=True)
    e.add_field(name="🛡️ Verified By", value=ctx.author.mention, inline=True)
    await ctx.send(embed=e, delete_after=10)

@bot.command(name="end")
@commands.guild_only()
async def end_cmd(ctx, message_id: int):
    if not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass

    g = next(
        (x for x in DATA["giveaways"].values()
         if int(x.get("guild_id")) == ctx.guild.id and int(x.get("message_id", 0)) == message_id),
        None
    )
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)

    g["ends_at"] = 0
    DATA["giveaways"][g["_id"]] = g
    save_data()
    await finish_giveaway(g)

@bot.command(name="reroll")
@commands.guild_only()
async def reroll_cmd(ctx, message_id: int):
    if not is_staff(ctx.author):
        return
    try:
        await ctx.message.delete()
    except Exception:
        pass

    g = next(
        (x for x in DATA["giveaways"].values()
         if int(x.get("guild_id")) == ctx.guild.id and int(x.get("message_id", 0)) == message_id),
        None
    )
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)

    old_users = {
        int(w["user_id"])
        for w in DATA["winners"].values()
        if w.get("giveaway_id") == g["_id"]
    }
    replacement = choose_winners(ctx.guild, g, 1, old_users)
    if not replacement:
        return await ctx.send("❌ No eligible replacement.", delete_after=8)

    m = replacement[0]
    wid = new_id()
    DATA["winners"][wid] = {
        "_id": wid,
        "giveaway_id": g["_id"],
        "guild_id": ctx.guild.id,
        "user_id": m.id,
        "status": "pending",
        "deadline_at": now() + int(g.get("claim_seconds", 1800)),
        "created_at": now()
    }
    g["status"] = "claiming"
    DATA["giveaways"][g["_id"]] = g
    await add_role(m, get_role(ctx.guild, g.get("winner_role_id")))
    save_data()
    await ctx.send(f"🔄 New winner: {m.mention}", delete_after=10)

@bot.command(name="participants")
@commands.guild_only()
async def participants_cmd(ctx, message_id: int):
    if not is_staff(ctx.author):
        return
    g = next(
        (x for x in DATA["giveaways"].values()
         if int(x.get("guild_id")) == ctx.guild.id and int(x.get("message_id", 0)) == message_id),
        None
    )
    if not g:
        return await ctx.send("❌ Giveaway not found.", delete_after=8)
    await ctx.send(
        f"👥 **{len(participants(g['_id']))} participants** in **{g['prize']}**.",
        delete_after=10
    )

@bot.command(name="invites")
@commands.guild_only()
async def invites_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    await ctx.send(
        f"👥 {member.mention} has **{invite_count(ctx.guild.id, member.id)}** valid tracked invite(s)."
    )

@bot.command(name="invite-add")
@commands.guild_only()
async def invite_add(ctx, member: discord.Member, amount: int):
    if not is_staff(ctx.author):
        return
    if amount <= 0:
        return
    key = f"{ctx.guild.id}:{member.id}"
    DATA["invites"][key] = invite_count(ctx.guild.id, member.id) + amount
    save_data()
    await ctx.send(f"✅ Added **{amount}** invite(s) to {member.mention}.", delete_after=8)

@bot.command(name="invite-set")
@commands.guild_only()
async def invite_set(ctx, member: discord.Member, amount: int):
    if not is_staff(ctx.author):
        return
    key = f"{ctx.guild.id}:{member.id}"
    DATA["invites"][key] = max(0, amount)
    save_data()
    await ctx.send(
        f"✅ Set {member.mention}'s invites to **{max(0, amount)}**.",
        delete_after=8
    )

@bot.command(name="setmanager")
@commands.guild_only()
async def setmanager(ctx, role: discord.Role):
    if not ctx.author.guild_permissions.administrator:
        return
    DATA["settings"].setdefault(str(ctx.guild.id), {})["manager_role_id"] = role.id
    save_data()
    await ctx.send(f"✅ Manager role set to {role.mention}.", delete_after=8)

@bot.command(name="extra-role")
@commands.guild_only()
async def extra_role(ctx, role: discord.Role, entries: int):
    if not is_staff(ctx.author):
        return
    DATA["extra_roles"].setdefault(str(ctx.guild.id), {})[str(role.id)] = max(0, entries)
    save_data()
    await ctx.send(
        f"✅ {role.mention} now gives **+{max(0, entries)}** extra entry/entries.",
        delete_after=8
    )

@bot.command(name="extra-remove")
@commands.guild_only()
async def extra_remove(ctx, role: discord.Role):
    if not is_staff(ctx.author):
        return
    DATA["extra_roles"].setdefault(str(ctx.guild.id), {}).pop(str(role.id), None)
    save_data()
    await ctx.send(f"✅ Removed extra entries from {role.mention}.", delete_after=8)

# -------------------- Slash groups --------------------

class GiveawayGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="giveaway", description="Giveaway management")

    @app_commands.command(name="start", description="Start a giveaway")
    async def start(
        self,
        interaction: discord.Interaction,
        prize: str,
        winners: int,
        duration: str,
        claim_minutes: int = 30,
        winner_role: discord.Role = None,
        required_role: discord.Role = None,
        bypass_role: discord.Role = None,
        min_invites: int = 0,
        booster_required: bool = False
    ):
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        if winners < 1:
            return await interaction.response.send_message("❌ Winners must be at least 1.", ephemeral=True)
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        gid = new_id()
        g = {
            "_id": gid,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message_id": 0,
            "prize": prize,
            "winner_count": winners,
            "ends_at": now() + seconds,
            "claim_seconds": max(60, claim_minutes * 60),
            "winner_role_id": winner_role.id if winner_role else 0,
            "required_role_id": required_role.id if required_role else 0,
            "bypass_role_id": bypass_role.id if bypass_role else 0,
            "min_invites": max(0, min_invites),
            "booster_required": booster_required,
            "status": "active",
            "created_by": interaction.user.id,
            "created_at": now()
        }

        DATA["giveaways"][gid] = g
        save_data()

        await interaction.response.send_message("Creating giveaway...", ephemeral=True)
        msg = await interaction.channel.send(
            embed=giveaway_embed(g, interaction.guild),
            view=GiveawayView(gid)
        )
        g["message_id"] = msg.id
        DATA["giveaways"][gid] = g
        save_data()
        bot.add_view(GiveawayView(gid))

    @app_commands.command(name="end", description="End a giveaway now")
    async def end(self, interaction: discord.Interaction, message_id: str):
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        g = next(
            (x for x in DATA["giveaways"].values()
             if int(x.get("guild_id")) == interaction.guild.id and int(x.get("message_id", 0)) == mid),
            None
        )
        if not g:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
        g["ends_at"] = 0
        DATA["giveaways"][g["_id"]] = g
        save_data()
        await interaction.response.send_message("✅ Giveaway ended.", ephemeral=True)
        await finish_giveaway(g)

    @app_commands.command(name="reroll", description="Reroll a giveaway")
    async def reroll(self, interaction: discord.Interaction, message_id: str):
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)

        g = next(
            (x for x in DATA["giveaways"].values()
             if int(x.get("guild_id")) == interaction.guild.id and int(x.get("message_id", 0)) == mid),
            None
        )
        if not g:
            return await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)

        used = {
            int(w["user_id"])
            for w in DATA["winners"].values()
            if w.get("giveaway_id") == g["_id"]
        }
        replacement = choose_winners(interaction.guild, g, 1, used)
        if not replacement:
            return await interaction.response.send_message("❌ No eligible replacement.", ephemeral=True)

        m = replacement[0]
        wid = new_id()
        DATA["winners"][wid] = {
            "_id": wid,
            "giveaway_id": g["_id"],
            "guild_id": interaction.guild.id,
            "user_id": m.id,
            "status": "pending",
            "deadline_at": now() + int(g.get("claim_seconds", 1800)),
            "created_at": now()
        }
        g["status"] = "claiming"
        DATA["giveaways"][g["_id"]] = g
        await add_role(m, get_role(interaction.guild, g.get("winner_role_id")))
        save_data()
        await interaction.response.send_message(f"🔄 New winner: {m.mention}")

class InviteRewardGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="invite-reward", description="Invite rewards")

    @app_commands.command(name="create", description="Create an invite reward")
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        required_invites: int,
        duration: str,
        reward_role: discord.Role = None
    ):
        if not interaction.guild or not is_staff(interaction.user):
            return await interaction.response.send_message("❌ Staff only.", ephemeral=True)
        try:
            seconds = parse_duration(duration)
        except ValueError as e:
            return await interaction.response.send_message(f"❌ {e}", ephemeral=True)

        rid = new_id()
        r = {
            "_id": rid,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "prize": prize,
            "required_invites": max(0, required_invites),
            "ends_at": now() + seconds,
            "reward_role_id": reward_role.id if reward_role else 0,
            "status": "active",
            "created_by": interaction.user.id
        }
        DATA["invite_rewards"][rid] = r
        save_data()

        e = discord.Embed(title="🎁 Invite Reward", color=discord.Color.green())
        e.add_field(name="🎁 Prize", value=prize, inline=True)
        e.add_field(name="👥 Required Invites", value=str(required_invites), inline=True)
        e.add_field(name="⏰ Ends", value=f"<t:{r['ends_at']}:F>", inline=True)
        if reward_role:
            e.add_field(name="🏷️ Reward Role", value=reward_role.mention, inline=True)

        await interaction.response.send_message(
            embed=e,
            view=InviteRewardView(rid)
        )
        bot.add_view(InviteRewardView(rid))

bot.tree.add_command(GiveawayGroup())
bot.tree.add_command(InviteRewardGroup())

# -------------------- Error handler --------------------

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            f"❌ Missing argument. Use `{PREFIX}help`.",
            delete_after=8
        )
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(
            f"❌ Invalid argument. Use `{PREFIX}help`.",
            delete_after=8
        )
        return
    print("COMMAND ERROR:", repr(error))

@bot.command(name="help")
async def help_cmd(ctx):
    e = discord.Embed(title="🤖 Giveaway Bot Commands", color=discord.Color.blurple())
    e.description = (
        f"**Prefix:** `{PREFIX}`\n\n"
        f"`{PREFIX} claim <message_id>` — verify/claim a winner\n"
        f"`{PREFIX} end <message_id>` — end giveaway\n"
        f"`{PREFIX} reroll <message_id>` — reroll\n"
        f"`{PREFIX} participants <message_id>` — participant count\n"
        f"`{PREFIX} invites [@user]` — invite count\n"
        f"`{PREFIX} invite-add @user <amount>` — staff adjustment\n"
        f"`{PREFIX} invite-set @user <amount>` — staff adjustment\n"
        f"`{PREFIX} setmanager @role` — set manager role\n"
        f"`{PREFIX} extra-role @role <entries>` — add extra entries\n"
        f"`{PREFIX} extra-remove @role` — remove extra entries\n\n"
        f"Slash: `/giveaway start`, `/giveaway end`, `/giveaway reroll`, "
        f"`/invite-reward create`"
    )
    await ctx.send(embed=e)

print("Starting Temalix-safe bot...")
bot.run(TOKEN)
