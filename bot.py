import os, re, random, string, asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN=os.getenv('DISCORD_TOKEN')
TEST_GUILD_ID=os.getenv('TEST_GUILD_ID')
TICKET_CHANNEL_ID=1511693319611224115
if not TOKEN: raise RuntimeError('DISCORD_TOKEN environment variable is missing.')

def now(): return datetime.now(timezone.utc)
def duration(s):
    m=re.fullmatch(r'\s*(\d+)\s*([smhdw])\s*',s.lower())
    if not m:return None
    return {'s':timedelta(seconds=int(m[1])),'m':timedelta(minutes=int(m[1])),'h':timedelta(hours=int(m[1])),'d':timedelta(days=int(m[1])),'w':timedelta(weeks=int(m[1]))}[m[2]]
def rid(s):
    m=re.search(r'\d{15,25}',str(s)); return int(m[0]) if m else None
def claimid(): return 'CLM-'+''.join(random.choices(string.ascii_uppercase+string.digits,k=8))
def color(s):
    s=s.strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}',s): raise ValueError('Use a 6-digit hex color, e.g. #5865F2.')
    return int(s,16)

@dataclass
class GW:
    mid:int; cid:int; gid:int; prize:str; winners:int; ends:datetime; claim:str
    req_inv:int=0; req_role:int|None=None; bypass:int|None=None; win_role:int|None=None
    extra:dict=field(default_factory=dict); say:str|None=None; say_channel:int|None=None; react:str='☑️'
    ping_min:int=0; ping_role:int|None=None; desc:str|None=None; image:str|None=None
    users:set=field(default_factory=set); completed:set=field(default_factory=set); ended:bool=False; pinged:bool=False
@dataclass
class Template:
    name:str; prize:str; winners:int; dur:str; claim:str
@dataclass
class Reward:
    id:str; required:int; text:str; channel:int

GWS={}; TEMPLATES={}; REWARDS={}; INVITES={}; CLAIMS={}
class CFG:
    status='online'; activity='playing'; activity_text='Giveaways'; nickname=None; embed_color=0x5865F2
    footer='Professional Giveaway System'; branding='Giveaway Bot'
cfg=CFG(); invite_cache={}

intents=discord.Intents.default(); intents.members=True; intents.messages=True; intents.message_content=True; intents.guilds=True
class Bot(commands.Bot):
    def __init__(self): super().__init__(command_prefix=commands.when_mentioned,intents=intents,allowed_mentions=discord.AllowedMentions(users=True,roles=True,everyone=False))
    async def setup_hook(self):
        if TEST_GUILD_ID:
            g=discord.Object(id=int(TEST_GUILD_ID)); self.tree.copy_global_to(guild=g); await self.tree.sync(guild=g)
        else: await self.tree.sync()
        loop.start()
bot=Bot()

def embed(title,desc=None,colour=None):
    e=discord.Embed(title=title,description=desc,color=colour or cfg.embed_color); e.set_footer(text=cfg.footer); return e
def role(g,r): return g.get_role(r).mention if r and g.get_role(r) else (f'<@&{r}>' if r else 'None')
def entries(g,m): return 1+sum(g.extra.get(r.id,0) for r in m.roles)
def eligible(g,m):
    bypass=g.bypass and m.get_role(g.bypass)
    if g.req_role and not m.get_role(g.req_role) and not bypass:return False,'You do not have the required role.'
    if g.req_inv and INVITES.get(m.id,0)<g.req_inv and not bypass:return False,f'You need at least **{g.req_inv} invites**.'
    if g.say and m.id not in g.completed and not bypass:return False,'You have not completed the required message.'
    return True,''
def public(g,G):
    e=embed(f'🎉 {g.prize}')
    if g.desc:e.description=g.desc
    e.add_field(name='How to enter',value='Click the **Enter Giveaway** button below.',inline=False)
    e.add_field(name='Winners',value=str(g.winners),inline=True)
    e.add_field(name='Ends',value=f'<t:{int(g.ends.timestamp())}:R>\n<t:{int(g.ends.timestamp())}:f>',inline=True)
    if g.extra:e.add_field(name='Extra Entries',value='\n'.join(f'{role(G,r)}  **+{n} entries**' for r,n in g.extra.items()),inline=False)
    req=[]
    if g.req_role:req.append(f'Have the role: {role(G,g.req_role)}')
    if g.req_inv:req.append(f'Required invites: **{g.req_inv}**')
    if g.say:req.append(f'Say `{g.say}` in <#{g.say_channel}> — react {g.react}')
    e.add_field(name='Requirements',value='\n'.join(req) or 'None',inline=False)
    if g.win_role:e.add_field(name='Winner will get the role',value=role(G,g.win_role),inline=False)
    e.add_field(name='Ends at',value=f'<t:{int(g.ends.timestamp())}:F>',inline=False)
    if g.image:e.set_image(url=g.image)
    return e

def choose(g):
    pool=[]; G=bot.get_guild(g.gid)
    for u in g.users:
        m=G.get_member(u) if G else None
        if not m:continue
        ok,_=eligible(g,m)
        if ok: pool += [u]*entries(g,m)
    out=[]
    while pool and len(out)<g.winners:
        u=random.choice(pool); out.append(u); pool=[x for x in pool if x!=u]
    return out

class GWView(discord.ui.View):
    def __init__(self,mid): super().__init__(timeout=None); self.mid=mid
    @discord.ui.button(label='Enter Giveaway',style=discord.ButtonStyle.success,emoji='🎉',custom_id='gw_enter')
    async def enter(self,i,b):
        g=GWS.get(self.mid)
        if not g or g.ended:return await i.response.send_message(embed=embed('Giveaway ended','This giveaway is no longer active.'),ephemeral=True)
        if not isinstance(i.user,discord.Member):return
        ok,why=eligible(g,i.user)
        if not ok:return await i.response.send_message(embed=embed('Entry requirements not met',why),ephemeral=True)
        g.users.add(i.user.id); e=embed('Entry confirmed','Your giveaway entry has been registered.')
        e.add_field(name='Your Entries',value=str(entries(g,i.user)),inline=True); e.add_field(name='Participants',value=str(len(g.users)),inline=True)
        await i.response.send_message(embed=e,ephemeral=True)
    @discord.ui.button(label='Participants',style=discord.ButtonStyle.secondary,emoji='👥',custom_id='gw_people')
    async def people(self,i,b):
        g=GWS.get(self.mid)
        if not g:return await i.response.send_message(embed=embed('Unavailable','Giveaway data is unavailable.'),ephemeral=True)
        lines=[]
        for u in list(g.users)[:80]:
            m=i.guild.get_member(u); lines.append(f'{m.mention if m else f"<@{u}>"} — **{entries(g,m) if m else 1} entries**')
        e=embed('Giveaway Participants','\n'.join(lines) or 'No participants.')
        if isinstance(i.user,discord.Member):e.add_field(name='Your Entries',value=str(entries(g,i.user)),inline=True)
        await i.response.send_message(embed=e,ephemeral=True)
class EndView(discord.ui.View):
    def __init__(self):super().__init__(timeout=None); self.add_item(discord.ui.Button(label='Giveaway Ended',style=discord.ButtonStyle.secondary,disabled=True))

class CreateModal(discord.ui.Modal,title='Create Giveaway'):
    prize=discord.ui.TextInput(label='Prize',max_length=100)
    winners=discord.ui.TextInput(label='Winners',default='1',max_length=3)
    dur=discord.ui.TextInput(label='Duration',default='1h',placeholder='10m / 2h / 1d')
    claim=discord.ui.TextInput(label='Claim Time',default='24h',required=False)
    inv=discord.ui.TextInput(label='Required Invites',default='0',required=False)
    async def on_submit(self,i):
        try:w=max(1,int(str(self.winners))); inv=max(0,int(str(self.inv or 0)))
        except:return await i.response.send_message(embed=embed('Invalid setup','Winners and invites must be numbers.'),ephemeral=True)
        if not duration(str(self.dur)):return await i.response.send_message(embed=embed('Invalid duration','Use 10m, 2h, 1d, etc.'),ephemeral=True)
        await i.response.send_message(embed=embed('Giveaway setup','Configure advanced options, then preview and confirm.'),view=Options(i.user.id,i.channel_id,i.guild_id,str(self.prize),w,str(self.dur),str(self.claim or '24h'),inv),ephemeral=True)

class Options(discord.ui.View):
    def __init__(self,uid,cid,gid,prize,w,dur,claim,inv):
        super().__init__(timeout=600); self.uid=uid; self.cid=cid; self.gid=gid; self.prize=prize; self.w=w; self.dur=dur; self.claim=claim; self.inv=inv
        self.req_role=self.bypass=self.win_role=self.say_channel=self.ping_role=None; self.extra={}; self.say=None; self.react='☑️'; self.ping_min=0; self.desc=self.image=None
    async def interaction_check(self,i):
        if i.user.id!=self.uid:await i.response.send_message(embed=embed('Not your setup','Only the creator can configure this giveaway.'),ephemeral=True);return False
        return True
    @discord.ui.button(label='Requirements',style=discord.ButtonStyle.primary)
    async def req(self,i,b):await i.response.send_modal(ReqModal(self))
    @discord.ui.button(label='Roles & Entries',style=discord.ButtonStyle.secondary)
    async def roles(self,i,b):await i.response.send_modal(RoleModal(self))
    @discord.ui.button(label='Auto-Ping',style=discord.ButtonStyle.secondary)
    async def ping(self,i,b):await i.response.send_modal(PingModal(self))
    @discord.ui.button(label='Details',style=discord.ButtonStyle.secondary)
    async def details(self,i,b):await i.response.send_modal(DetailModal(self))
    @discord.ui.button(label='Preview',style=discord.ButtonStyle.secondary)
    async def preview(self,i,b):
        g=GW(0,self.cid,self.gid,self.prize,self.w,now()+duration(self.dur),self.claim,self.inv,self.req_role,self.bypass,self.win_role,self.extra,self.say,self.say_channel,self.react,self.ping_min,self.ping_role,self.desc,self.image)
        await i.response.send_message(embed=public(g,i.guild),view=Confirm(self),ephemeral=True)
class ReqModal(discord.ui.Modal,title='Giveaway Requirements'):
    def __init__(self,p):super().__init__();self.p=p
    req_role=discord.ui.TextInput(label='Required Role ID or Mention',required=False)
    bypass=discord.ui.TextInput(label='Bypass Role ID or Mention',required=False)
    say=discord.ui.TextInput(label='Requirement to Say',required=False,placeholder='Example: jamil is ragebaiter')
    channel=discord.ui.TextInput(label='Requirement Channel ID',required=False)
    reaction=discord.ui.TextInput(label='Reaction',default='☑️',required=False)
    async def on_submit(self,i):
        self.p.req_role=rid(self.req_role);self.p.bypass=rid(self.bypass);self.p.say=str(self.say).strip() or None;self.p.say_channel=rid(self.channel);r=str(self.reaction).strip();self.p.react=r if r in ('☑️','✅','✔️') else '☑️'
        await i.response.send_message(embed=embed('Requirements saved','The giveaway requirement settings were updated.'),ephemeral=True)
class RoleModal(discord.ui.Modal,title='Roles & Extra Entries'):
    def __init__(self,p):super().__init__();self.p=p
    winner=discord.ui.TextInput(label='Winner Role ID or Mention',required=False)
    extra=discord.ui.TextInput(label='Extra Entry Roles',required=False,placeholder='role_id:+2, role_id:+5')
    async def on_submit(self,i):
        self.p.win_role=rid(self.winner);self.p.extra={}
        for x in str(self.extra).split(','):
            if ':' not in x:continue
            a,n=x.split(':',1); r=rid(a)
            try:n=int(n.replace('+','').strip())
            except:continue
            if r and n>0:self.p.extra[r]=n
        await i.response.send_message(embed=embed('Roles saved','Winner and extra-entry roles were updated.'),ephemeral=True)
class PingModal(discord.ui.Modal,title='Automatic End Ping'):
    def __init__(self,p):super().__init__();self.p=p
    mins=discord.ui.TextInput(label='Minutes Before End',default='0')
    rolex=discord.ui.TextInput(label='Ping Role ID or Mention',required=False)
    async def on_submit(self,i):
        try:self.p.ping_min=max(0,int(str(self.mins)))
        except:self.p.ping_min=0
        self.p.ping_role=rid(self.rolex);await i.response.send_message(embed=embed('Auto-ping saved','Automatic end notification configured.'),ephemeral=True)
class DetailModal(discord.ui.Modal,title='Giveaway Details'):
    def __init__(self,p):super().__init__();self.p=p
    desc=discord.ui.TextInput(label='Description',required=False,style=discord.TextStyle.paragraph,max_length=1000)
    image=discord.ui.TextInput(label='Image URL',required=False)
    async def on_submit(self,i):self.p.desc=str(self.desc).strip() or None;self.p.image=str(self.image).strip() or None;await i.response.send_message(embed=embed('Details saved','Description and image updated.'),ephemeral=True)
class Confirm(discord.ui.View):
    def __init__(self,p):super().__init__(timeout=600);self.p=p
    @discord.ui.button(label='Confirm & Create',style=discord.ButtonStyle.success)
    async def yes(self,i,b):
        ch=i.guild.get_channel(self.p.cid)
        g=GW(0,ch.id,i.guild.id,self.p.prize,self.p.w,now()+duration(self.p.dur),self.p.claim,self.p.inv,self.p.req_role,self.p.bypass,self.p.win_role,self.p.extra,self.p.say,self.p.say_channel,self.p.react,self.p.ping_min,self.p.ping_role,self.p.desc,self.p.image)
        msg=await ch.send(embed=public(g,i.guild));g.mid=msg.id;GWS[msg.id]=g;await msg.edit(view=GWView(msg.id));await i.response.edit_message(embed=embed('Giveaway created',f'Posted in {ch.mention}.'),view=None)
    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.danger)
    async def no(self,i,b):await i.response.edit_message(embed=embed('Giveaway cancelled','No giveaway was created.'),view=None)

# Commands
GWG=app_commands.Group(name='giveaway',description='Professional giveaway management')
TG=app_commands.Group(name='template',description='Giveaway templates',parent=GWG)
BG=app_commands.Group(name='bot',description='Bot customization')
IG=app_commands.Group(name='invite',description='Invite tracking and rewards')
RG=app_commands.Group(name='reward',description='Invite rewards',parent=IG)

@GWG.command(name='create',description='Create a professional giveaway')
@app_commands.checks.has_permissions(manage_guild=True)
async def gw_create(i):await i.response.send_modal(CreateModal())
@GWG.command(name='end',description='End a giveaway early')
@app_commands.checks.has_permissions(manage_guild=True)
async def gw_end(i,message_id:str):
    try:g=GWS[int(message_id)]
    except:return await i.response.send_message(embed=embed('Not found','Giveaway not found.'),ephemeral=True)
    await finish(g);await i.response.send_message(embed=embed('Giveaway ended','The giveaway was processed.'),ephemeral=True)
@GWG.command(name='reroll',description='Reroll winners')
@app_commands.checks.has_permissions(manage_guild=True)
async def gw_reroll(i,message_id:str,winners:int=1):
    try:g=GWS[int(message_id)]
    except:return await i.response.send_message(embed=embed('Not found','Giveaway not found.'),ephemeral=True)
    ws=choose(g)[:max(1,min(20,winners))];await i.response.send_message(embed=embed('Giveaway Reroll','\n'.join(f'<@{x}>' for x in ws) or 'No eligible winners.'))
@GWG.command(name='participants',description='View giveaway participants')
async def gw_part(i,message_id:str):
    try:g=GWS[int(message_id)]
    except:return await i.response.send_message(embed=embed('Not found','Giveaway not found.'),ephemeral=True)
    txt='\n'.join(f'<@{u}>' for u in list(g.users)[:100]) or 'No participants.';await i.response.send_message(embed=embed('Giveaway Participants',txt[:4000]),ephemeral=True)

@TG.command(name='create',description='Create a reusable giveaway template')
@app_commands.checks.has_permissions(manage_guild=True)
async def tpl_create(i):await i.response.send_modal(TplModal())
class TplModal(discord.ui.Modal,title='Create Giveaway Template'):
    name=discord.ui.TextInput(label='Template Name');prize=discord.ui.TextInput(label='Prize');w=discord.ui.TextInput(label='Winners',default='1');dur=discord.ui.TextInput(label='Duration',default='1h');claim=discord.ui.TextInput(label='Claim Time',default='24h')
    async def on_submit(self,i):
        try:w=max(1,int(str(self.w)))
        except:w=1
        TEMPLATES[str(self.name).lower()]=Template(str(self.name),str(self.prize),w,str(self.dur),str(self.claim));await i.response.send_message(embed=embed('Template created',f'**{self.name}** is ready.'),ephemeral=True)
@TG.command(name='list',description='List giveaway templates')
async def tpl_list(i):await i.response.send_message(embed=embed('Giveaway Templates','\n'.join(f'• **{x.name}** — {x.prize}' for x in TEMPLATES.values()) or 'No templates.'),ephemeral=True)
@TG.command(name='delete',description='Delete a template')
@app_commands.checks.has_permissions(manage_guild=True)
async def tpl_delete(i,name:str):await i.response.send_message(embed=embed('Template','Deleted.' if TEMPLATES.pop(name.lower(),None) else 'Template not found.'),ephemeral=True)

@IG.command(name='count',description='View your tracked invites')
async def inv_count(i):await i.response.send_message(embed=embed('Invite Count',f'You have **{INVITES.get(i.user.id,0)} tracked invites**.'),ephemeral=True)
@IG.command(name='set',description='Set a member invite count')
@app_commands.checks.has_permissions(manage_guild=True)
async def inv_set(i,member:discord.Member,count:int):INVITES[member.id]=max(0,count);await i.response.send_message(embed=embed('Invite count updated',f'{member.mention}: **{max(0,count)}**'),ephemeral=True)
@RG.command(name='create',description='Create an invite reward')
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_create(i):await i.response.send_modal(RewardModal())
class RewardModal(discord.ui.Modal,title='Create Invite Reward'):
    req=discord.ui.TextInput(label='Required Invites',default='5');reward=discord.ui.TextInput(label='Reward');channel=discord.ui.TextInput(label='Claim Channel ID',default=str(TICKET_CHANNEL_ID))
    async def on_submit(self,i):
        try:n=max(1,int(str(self.req)));c=rid(self.channel) or TICKET_CHANNEL_ID
        except:return await i.response.send_message(embed=embed('Invalid setup','Required invites must be a number.'),ephemeral=True)
        x='IR-'+''.join(random.choices(string.ascii_uppercase+string.digits,k=6));REWARDS[x]=Reward(x,n,str(self.reward),c);await i.response.send_message(embed=embed('Invite reward created',f'Reward ID: `{x}`'),ephemeral=True)
@RG.command(name='claim',description='Claim an invite reward')
async def reward_claim(i,reward_id:str):
    r=REWARDS.get(reward_id.upper())
    if not r:return await i.response.send_message(embed=embed('Unavailable','Reward not found.'),ephemeral=True)
    c=INVITES.get(i.user.id,0)
    if c<r.required:return await i.response.send_message(embed=embed('Requirements not met',f'Need **{r.required}** invites; you have **{c}**.'),ephemeral=True)
    x=claimid();CLAIMS[x]={'user':i.user.id,'reward':r.text,'reward_id':r.id};await i.response.send_message(embed=embed('Reward Claim Created',f'**Claim ID:** `{x}`\n\nSend this ID in your giveaway ticket so staff can verify your claim.\nCreate your ticket in <#{r.channel}>.'),ephemeral=True)
@RG.command(name='check',description='Check an invite reward claim')
@app_commands.checks.has_permissions(manage_guild=True)
async def reward_check(i,claim_id:str):
    c=CLAIMS.get(claim_id.upper())
    if not c:return await i.response.send_message(embed=embed('Claim not found','No claim exists with that ID.'),ephemeral=True)
    await i.response.send_message(embed=embed('Claim Verification',f'**Claim:** `{claim_id.upper()}`\n**Member:** <@{c["user"]}>\n**Reward:** {c["reward"]}\n**Status:** Pending staff verification'),ephemeral=True)

@BG.command(name='status',description='Set bot status')
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.choices(status=[app_commands.Choice(name='Online',value='online'),app_commands.Choice(name='Idle',value='idle'),app_commands.Choice(name='Do Not Disturb',value='dnd'),app_commands.Choice(name='Invisible',value='invisible')])
async def bot_status(i,status:app_commands.Choice[str]):cfg.status=status.value;await presence();await i.response.send_message(embed=embed('Bot status updated',f'Status: **{status.name}**'),ephemeral=True)
@BG.command(name='activity',description='Set bot activity')
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.choices(activity=[app_commands.Choice(name='Playing',value='playing'),app_commands.Choice(name='Watching',value='watching'),app_commands.Choice(name='Listening',value='listening'),app_commands.Choice(name='Streaming',value='streaming')])
async def bot_activity(i,activity:app_commands.Choice[str]):await i.response.send_modal(ActivityModal(activity.value))
class ActivityModal(discord.ui.Modal,title='Set Bot Activity'):
    text=discord.ui.TextInput(label='Activity Text',max_length=128)
    def __init__(self,t):super().__init__();self.t=t
    async def on_submit(self,i):cfg.activity=self.t;cfg.activity_text=str(self.text);await presence();await i.response.send_message(embed=embed('Activity updated',f'{self.t.title()}: **{self.text}**'),ephemeral=True)
@BG.command(name='nickname',description='Set bot nickname')
@app_commands.checks.has_permissions(manage_nicknames=True)
async def bot_nick(i,nickname:str):await i.guild.me.edit(nick=nickname[:32] or None);cfg.nickname=nickname;await i.response.send_message(embed=embed('Nickname updated',nickname or 'Default'),ephemeral=True)
@BG.command(name='embed-color',description='Set embed color')
@app_commands.checks.has_permissions(manage_guild=True)
async def bot_color(i,value:str):
    try:cfg.embed_color=color(value)
    except ValueError as e:return await i.response.send_message(embed=embed('Invalid color',str(e)),ephemeral=True)
    await i.response.send_message(embed=embed('Embed color updated',f'#{cfg.embed_color:06X}'),ephemeral=True)
@BG.command(name='footer',description='Set embed footer')
@app_commands.checks.has_permissions(manage_guild=True)
async def bot_footer(i,text:str):cfg.footer=text[:2048];await i.response.send_message(embed=embed('Footer updated',cfg.footer),ephemeral=True)
@BG.command(name='branding',description='Set bot branding')
@app_commands.checks.has_permissions(manage_guild=True)
async def bot_brand(i,name:str):cfg.branding=name[:80];await i.response.send_message(embed=embed('Branding updated',cfg.branding),ephemeral=True)
@BG.command(name='settings',description='View bot settings')
async def bot_settings(i):await i.response.send_message(embed=embed('Bot Settings',f'**Status:** {cfg.status.title()}\n**Activity:** {cfg.activity.title()} {cfg.activity_text}\n**Color:** #{cfg.embed_color:06X}\n**Footer:** {cfg.footer}\n**Active Giveaways:** {len(GWS)}\n**Templates:** {len(TEMPLATES)}\n**Invite Rewards:** {len(REWARDS)}'),ephemeral=True)

async def presence():
    st={'online':discord.Status.online,'idle':discord.Status.idle,'dnd':discord.Status.dnd,'invisible':discord.Status.invisible}.get(cfg.status,discord.Status.online)
    if cfg.activity=='playing':a=discord.Game(name=cfg.activity_text)
    elif cfg.activity=='watching':a=discord.Activity(type=discord.ActivityType.watching,name=cfg.activity_text)
    elif cfg.activity=='listening':a=discord.Activity(type=discord.ActivityType.listening,name=cfg.activity_text)
    else:a=discord.Streaming(name=cfg.activity_text,url='https://twitch.tv/')
    await bot.change_presence(status=st,activity=a)

@bot.event
async def on_message(m):
    if m.author.bot or not m.guild:return
    for g in list(GWS.values()):
        if g.ended or not g.say:continue
        if g.say_channel and m.channel.id!=g.say_channel:continue
        if m.content.strip().casefold()!=g.say.casefold():continue
        if g.req_role and not m.author.get_role(g.req_role) and not (g.bypass and m.author.get_role(g.bypass)):continue
        g.completed.add(m.author.id)
        try:await m.add_reaction(g.react)
        except:pass
        try:await m.channel.send(embed=embed('Requirement completed',f'{m.author.mention}, your required message has been verified.'),delete_after=8)
        except:pass

@bot.event
async def on_ready():
    await presence()
    for g in bot.guilds:
        try:invite_cache[g.id]={x.code:x.uses or 0 for x in await g.invites()}
        except:pass
    print(f'Logged in as {bot.user} | {len(bot.guilds)} guilds')
@bot.event
async def on_member_join(m):
    try:
        old=invite_cache.get(m.guild.id,{})
        inv=await m.guild.invites();new={x.code:x.uses or 0 for x in inv};invite_cache[m.guild.id]=new
        for x in inv:
            if x.uses>old.get(x.code,0) and x.inviter:
                INVITES[x.inviter.id]=INVITES.get(x.inviter.id,0)+1;break
    except:pass

async def finish(g):
    if g.ended:return
    g.ended=True;ws=choose(g);G=bot.get_guild(g.gid);ch=bot.get_channel(g.cid)
    if not isinstance(ch,discord.TextChannel):return
    try:m=await ch.fetch_message(g.mid);e=embed(f'Giveaway Ended • {g.prize}');e.add_field(name='Winner(s)',value=', '.join(f'<@{u}>' for u in ws) or 'No eligible winners.',inline=False);e.add_field(name='Participants',value=str(len(g.users)),inline=True);await m.edit(embed=e,view=EndView())
    except:pass
    if ws:
        e=embed(f'Congratulations • {g.prize}',', '.join(f'<@{u}>' for u in ws));e.add_field(name='Claim Time',value=g.claim or 'Contact staff')
        if g.win_role and G:
            r=G.get_role(g.win_role)
            if r:
                for u in ws:
                    mm=G.get_member(u)
                    if mm:
                        try:await mm.add_roles(r,reason='Giveaway winner')
                        except:pass
                e.add_field(name='Winner Role',value=r.mention)
        await ch.send(embed=e)

@tasks.loop(seconds=15)
async def loop():
    for g in list(GWS.values()):
        if g.ended:continue
        left=(g.ends-now()).total_seconds()
        if g.ping_min and not g.pinged and 0<left<=g.ping_min*60:
            g.pinged=True;ch=bot.get_channel(g.cid)
            if isinstance(ch,discord.TextChannel):
                p=role(ch.guild,g.ping_role) if g.ping_role else '@here'
                try:await ch.send(f'{p} **{g.prize}** giveaway ends <t:{int(g.ends.timestamp())}:R>.')
                except:pass
        if left<=0:await finish(g)

@bot.tree.error
async def errors(i,e):
    if isinstance(e,app_commands.MissingPermissions):msg='You do not have permission to use this command.'
    else:print('Command error:',repr(e));msg='The command could not be completed.'
    x=embed('Permission denied' if isinstance(e,app_commands.MissingPermissions) else 'Something went wrong',msg)
    if i.response.is_done():await i.followup.send(embed=x,ephemeral=True)
    else:await i.response.send_message(embed=x,ephemeral=True)

bot.tree.add_command(GWG);bot.tree.add_command(BG);bot.tree.add_command(IG)
bot.run(TOKEN)
