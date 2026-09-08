# Gambly — Virtual Casino Bot + Web Dashboard (single-server build)

Fake-currency ("chips") gambling bot for Discord, plus a proper web dashboard for managing it —
login with Discord, see the servers you manage, edit settings and balances. No real money
anywhere — pure economy game.

Rename "Gambly" to whatever you want via `BOT_NAME` in `.env` — it's not hardcoded anywhere.

**This build is locked to one server.** Set `GUILD_ID` in `.env` and the bot will refuse to sit
in (and immediately leave) any other server. It also adds a moderation toolkit, a ticket system, a
user-reporting system, and configurable logging (messages/mod actions/reports/withdrawals/
deposits/tickets) — all of it live-editable from a new **Moderation & Logging** tab in the
existing dashboard, no restart needed. The dashboard's original tabs (Overview, Players, Server
settings, Bounties, Activity log, Danger zone) are untouched.

For step-by-step hosting instructions (free dashboard hosting + putting the bot on Oracle Cloud),
see **[DEPLOYMENT.md](DEPLOYMENT.md)**.

## What's in here

```
bot.py                    the Discord bot itself (economy/casino games)
config_schema.py           single source of truth for what's in config.json + its defaults -
                            imported by both bot.py and the dashboard, so they can't drift apart
logging_utils.py            send_log() / is_staff() / ticket-settings lookups, used by the cogs
embeds.py                    shared embed builders (consistent look across every log type)
cog_utils.py                  lets every mod/ticket/report command share one implementation
                              between its slash and prefix versions instead of writing it twice
cogs/
  moderation.py             kick/ban/mute/warn/purge/slowmode/lock
  tickets.py                 ticket panel + private ticket channels + transcripts
  reports.py                  /report + right-click "Report Message"
  logging_events.py            message edit/delete logging
economy.json, ...          flat-file data the bot reads/writes (auto-created if missing)
config.json                 per-server settings - the dashboard's Moderation & Logging tab writes here
warnings.json                moderation warning history (auto-created)
dashboard/                  the Flask web dashboard — same structure as the original, extended
  app.py                     routes + API (added: moderation_config, channels)
  auth.py                    Discord OAuth2 login + access control (unchanged)
  data.py                     flat-file helpers (now delegates schema/defaults to config_schema.py)
  discord_api.py              calls to Discord's HTTP API (added: bot_fetch_channels)
  templates/, static/          pages, CSS, JS (added: Moderation & Logging tab)
run.py                      local dev entrypoint for the dashboard
wsgi.py                     production entrypoint (gunicorn)
requirements.txt
.env.example
Procfile                    for one-click hosts (Railway, Render, etc.)
DEPLOYMENT.md               free dashboard hosting + Oracle Cloud bot deployment walkthrough
```

## 1. Create the Discord application

You need one Discord application that provides both the bot and the OAuth2 login.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. No privileged intents are needed (everything is slash
   commands). This is your `DISCORD_TOKEN`.
3. **OAuth2 → General** tab → copy the **Client ID** (`DISCORD_CLIENT_ID`) and **Client Secret**
   (`DISCORD_CLIENT_SECRET`).
4. Same page, under **Redirects**, add the exact URL the dashboard will use, e.g.
   `http://localhost:5000/callback` while testing, and `https://yourdomain.com/callback` once
   deployed. This must match `DISCORD_REDIRECT_URI` in `.env` byte-for-byte (scheme, host, path,
   trailing slash — all of it).
5. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`, pick the permissions you
   want the bot to have → copy the generated permissions integer into `DISCORD_BOT_PERMISSIONS`
   in `.env` (this is only used to build the "Add to a server" button).

## 2. Configure

```
cp .env.example .env
```

Fill in `DISCORD_TOKEN`, `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, and
generate a session secret:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

paste that into `FLASK_SECRET_KEY`.

`BOT_ADMIN_IDS` is optional — see **Access model** below. Leave it blank unless you want it.

Also fill in the new single-server variable:
- `GUILD_ID` — **required.** Your server's ID (Developer Mode on → right-click server icon → Copy
  Server ID). Slash commands sync only to this server (near-instant), and the bot leaves anywhere
  else it ends up.

Everything else new (`STAFF_ROLE_ID`, `TICKET_CATEGORY_ID`, `TICKET_PING_ROLE_ID`, and the six
`*_LOG_CHANNEL_ID` variables) is **optional** — they're only a one-time default used the first
time this server's config is created. You can set them in `.env`, or just leave them blank and
configure everything from the dashboard's **Moderation & Logging** tab after the bot's up and
you've logged in once — either way works, and the dashboard is always the live source of truth
after that first run (changing `.env` later does nothing for a server that's already been seen).

## 3. Install and run

```
pip install -r requirements.txt

# terminal 1 — the bot
python bot.py

# terminal 2 — the dashboard
python run.py
```

Open `http://localhost:5000`, click **Continue with Discord**, and you'll land on your server
picker.

## Access model

This works the same way Carl-bot, Dyno, and Sapphire's dashboards do: **nobody gets special
hardcoded access.** When someone logs in, the dashboard asks Discord which servers that person is
in and what their permissions are there. Anyone with **Manage Server** (or Administrator, or
who's the server owner) sees that server in their picker and can configure the bot for it —
exactly the same bar Discord itself uses to show its own Settings cog. Someone without that
permission simply never sees the server.

There's one optional extra layer: `BOT_ADMIN_IDS` in `.env` is a comma-separated list of Discord
user IDs (you set it — nothing in the code names anyone). Accounts in that list additionally get
a `/admin` page with cross-server tools: global default settings, the personal/DM ledger, and
search-by-player across every server at once. This mirrors the private staff dashboards real bots
keep for their own operators. Leave the variable blank and that page doesn't exist for anyone,
including you.

## Dashboard features

Per server (for anyone who manages it):
- **Overview** — players, chips in circulation, total debt, defaulted loans, banned count, richest players.
- **Players** — search/edit each player's wallet and bank directly, relative +/- adjustments,
  full account reset, forgive a loan, ban/unban, promote/demote manager, bulk grant to everyone at once.
- **Server settings** — prefix, manager role (pulled live from the server's actual roles),
  jackpot, tax %, server pot, lottery pot, test mode.
- **Moderation & Logging** — staff role, ticket category, ticket ping role, and an on/off switch +
  channel picker for each of the six log types (message edits/deletes, moderation actions, user
  reports, withdrawals, deposits, ticket transcripts). Channel and role pickers are pulled live
  from the server, same as the manager-role dropdown.
- **Bounties** — set or clear a bounty on any player.
- **Activity log** — everything logged from Discord or the dashboard itself, searchable.
- **Danger zone** — remove the bot from that server.

Bot-admin only (if `BOT_ADMIN_IDS` is set): global economy defaults (`get_setting()` knobs),
cross-server player search, CSV export of every server at once, the personal/DM ledger.

## Hosting it

The dashboard is a normal Flask app; the bot is a normal long-running Python process. They share
the same JSON files, so they need to run on the same machine/volume (or you point both at a
shared network volume).

**Platform hosts (Railway, Render, Fly.io, etc.):** push this repo, set the `.env` values as
environment variables in the platform's dashboard, and use the two processes in `Procfile` — one
web process for the dashboard, one worker process for the bot. Set `FLASK_ENV=production`. Update
`DISCORD_REDIRECT_URI` (and the Redirect URL in the Developer Portal) to your real
`https://yourapp.example.com/callback`.

**Your own VPS:** put this behind nginx (or Caddy) for TLS, run the bot and dashboard as two
systemd services:

```ini
# /etc/systemd/system/gambly-bot.service
[Unit]
Description=Gambly discord bot
After=network.target
[Service]
WorkingDirectory=/opt/gambly
EnvironmentFile=/opt/gambly/.env
ExecStart=/opt/gambly/venv/bin/python bot.py
Restart=always
[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/gambly-dashboard.service
[Unit]
Description=Gambly web dashboard
After=network.target
[Service]
WorkingDirectory=/opt/gambly
EnvironmentFile=/opt/gambly/.env
ExecStart=/opt/gambly/venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 wsgi:app
Restart=always
[Install]
WantedBy=multi-user.target
```

then `systemctl enable --now gambly-bot gambly-dashboard` and reverse-proxy nginx to
`127.0.0.1:5000`.

## Bot commands
- `/balance [user]` — check chip balance
- `/daily` — claim chips every 24h
- `/give <user> <amount>` — transfer chips
- `/leaderboard` — top 10 richest in the server
- `/coinflip <amount> <heads|tails>` — 50/50, 2x payout
- `/slots <amount>` — 3-reel slot machine, jackpot on triple 7️⃣ (20x)
- `/blackjack <amount>` — hit/stand vs dealer, buttons included, blackjack pays 1.5x
- `/wheel <amount>` — spin a weighted wheel, 0x-10x
- `/keno <amount> <numbers>` — pick 1-5 numbers (1-40), payout scales with matches
- `/baccarat <amount> <player|banker|tie>` — simplified baccarat, banker pays 0.95x (5% commission), tie pays 8x
- `/rps <amount> <rock|paper|scissors>` — vs the house, win doubles your bet
- `/ladder <amount> <rungs 1-6>` — each rung doubles your bet, miss one and lose it all
- `/lottery` / `/buyticket <count>` — daily per-server drawing, one winner takes the pot
- `/reset [user]` — admin only, resets balance to 1000

## Moderation commands
Every command below works both ways — as a slash command (`/kick`) and as a prefix command
(`!kick`, or whatever prefix this server has set in the dashboard's Server settings tab — the
same setting the casino commands already use). Usable by server Administrators, or anyone with
the guild's configured staff role (dashboard: Moderation & Logging tab; ban/unban need the native
"Ban Members" permission specifically — administrators have this by default):
- `/kick` / `!kick <member> [reason]`, `/ban` / `!ban <member-or-user-id> [reason]` (slash also
  takes `delete_days`, prefix always leaves history alone), `/unban` / `!unban <user-id>`
- `/mute` / `!mute <member> <minutes> [reason]` (Discord's native timeout), `/unmute` / `!unmute <member>`
- `/warn` / `!warn <member> <reason>`, `/warnings` / `!warnings <member>` (last 15),
  `/clearwarnings` / `!clearwarnings <member>`
- `/purge` / `!purge <amount> [member]` — bulk-delete recent messages, optionally filtered to one member
- `/slowmode` / `!slowmode <seconds>` — set/disable this channel's slowmode
- `/lock` / `!lock` and `/unlock` / `!unlock` — stop/allow @everyone sending messages in the current channel

Every action above posts a log embed to the configured **mod** log channel, if that log type is
turned on. Someone can't kick/ban/mute a member with an equal or higher top role than their own
(standard Discord-style role hierarchy check), unless they're the server owner.

## Reports
- `/report` / `!report <member> <reason> [evidence]` — report someone directly. Slash takes
  evidence as an attachment option; prefix picks up whatever file (if any) you attached to the
  message itself.
- Right-click any message → **Apps → Report Message** — reports whoever sent that message, with
  the message's own content and a jump link attached automatically. (Right-click actions are a
  Discord UI feature with no prefix equivalent — same as in the original bot's era.)

Either way it posts to the configured **report** log channel. If that log type isn't turned on
yet, the person still gets a "submitted" confirmation (not their fault staff hasn't set it up),
but honestly told it may not have reached anyone.

## Message logging
Message edits and deletes post to the configured **message** log channel (before/after content
for edits, author/channel/content/attachments for deletes). Skips the bot's own messages and edits
where the visible text didn't actually change (embeds loading in, link unfurls) to keep it from
turning into noise. Off by default — turn it on from the dashboard if you want it; it can get busy
in an active server.

## Withdrawals & deposits
`/deposit` and `/withdraw` post a transaction embed (user, amount, wallet/bank balance after,
status) to the **deposit** / **withdraw** log channels respectively, if those log types are on.

## Ticket system
1. Run `/ticket-panel` / `!ticket-panel` (Manage Server permission) in whichever channel should
   host it — e.g. your `gambling-info` or a dedicated `#support` channel. Optionally pass a title
   and description to customize the panel text.
2. Members click **Open Ticket** → a private channel is created under the configured ticket
   category, visible only to them + the staff role (+ admins).
3. Staff can **Claim** or **Close** from buttons inside the ticket channel, or run `/close-ticket`
   / `!close-ticket`. Closing posts a full text transcript to the **ticket** log channel (if that
   log type is on) and deletes the channel.
4. `/add-to-ticket` / `!add-to-ticket <member>` pulls a third person into an open ticket (e.g.
   another witness).

Buttons are persistent — they keep working after a bot restart, no re-posting the panel needed.
Staff role, ticket category, and ping role are all set from the dashboard's Moderation & Logging
tab (or their one-time `.env` defaults).

## Being banned / defaulting on a loan
Only a manager running `/banuser` (or toggling it from the dashboard) blocks someone from the
games. Defaulting on a loan doesn't auto-lock you out of everything else — the debt still
compounds 5%/day in `check_loan()` until it's paid off, but you can still play to try to win your
way out of it.

## Data storage
Balances are stored in `economy.json` next to `bot.py` (auto-created). It's flat-file, fine for a
handful of small-to-medium servers. Bank (`/deposit`, `/withdraw`) is separate from wallet `bal`
— every bet only ever checks/spends `bal`. Wallet balance can go negative from the dashboard's
balance field (no floor there on purpose, for manual corrections), but nothing in the bot itself
pushes it below 0.

## Privacy policy & Privileged Intents
The dashboard now serves a real, bot-specific privacy policy at **`/privacy`** (e.g.
`https://yourdashboard.example.com/privacy`) — `dashboard/templates/privacy.html`. Paste that URL
into the Discord Developer Portal's Privacy Policy URL field, and into any Privileged Intent
request form. Set `PRIVACY_CONTACT` in `.env` to show a real contact email on that page instead of
"contact server staff."

`/deletemydata` / `!deletemydata` lets anyone permanently wipe their own stored economy account
and warning history — this exists specifically because Discord's Developer Policy requires apps
that collect user data to provide a deletion mechanism, and it's referenced from the privacy page.

If you ever have to fill out Discord's Privileged Intent request form (happens once an app crosses
10,000 reachable users - see `docs.discord.com/developers/gateway/getting-started-with-privileged-intent-review`):
this bot only actually uses **Server Members** and **Message Content** intents (`bot.py`'s
`intents.members`/`intents.message_content`) - it never enables `intents.presences`, so leave
**Presence Intent** unchecked on that form. Requesting an intent you don't use both slows down
review and goes against Discord's own data-minimization guidance.

## Tuning knobs
All at the top of `bot.py`, or editable live via the dashboard's global defaults (bot-admin only) /
per-server settings tab:
- `STARTING_BALANCE`, `DAILY_AMOUNT`, `DAILY_COOLDOWN`
- `SLOT_SYMBOLS` — dict of symbol: weight (lower weight = rarer = bigger payout)
- Slot/blackjack payout multipliers are in `slots_payout()` and the blackjack natural-21 line
