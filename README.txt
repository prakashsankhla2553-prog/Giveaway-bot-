TEMALIX PROFESSIONAL GIVEAWAY BOT

Slash commands only. No prefix commands. No MongoDB and no SQLite.

FEATURES
- /giveaway create: Discord modal setup + advanced configuration
- Professional giveaway embed
- Enter Giveaway + Participants buttons
- Winners, duration, claim time, required role, required invites
- Requirement to Say: exact phrase + selected channel + ☑️/✅/✔️ reaction
- Extra entry roles
- Bypass role
- Winner role assignment
- Auto-ping before ending
- Giveaway end and reroll
- Giveaway templates via /giveaway template create/list/delete
- Invite tracking and invite rewards
- Claim ID system; user is told to send Claim ID in ticket
- Default ticket channel: 1511693319611224115
- /bot status: Online, Idle, Do Not Disturb, Invisible
- /bot activity: Playing, Watching, Listening, Streaming + modal text
- /bot nickname, /bot embed-color, /bot footer, /bot branding, /bot settings

ENVIRONMENT
DISCORD_TOKEN=YOUR_BOT_TOKEN
TEST_GUILD_ID=YOUR_SERVER_ID (optional; useful for testing command sync)

TEMALIX NOTE
Temalix's documented environment uses a read-only filesystem, so this build keeps runtime data in memory. Active giveaways, templates, invite counts, claims, and customization reset if the bot restarts. A persistent external database would be required for permanent storage.

DISCORD INTENTS
Enable Server Members Intent and Message Content Intent in Developer Portal.

RECOMMENDED PERMISSIONS
View Channels, Send Messages, Embed Links, Read Message History, Add Reactions, Manage Roles, Manage Nicknames. Invite tracking also requires access to server invites.
