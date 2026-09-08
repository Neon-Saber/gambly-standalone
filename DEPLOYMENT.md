# Deployment Guide

Two separate things get deployed here, and they don't need to live in the same place:

- **The bot** (`bot.py`) — a long-running process, needs to stay up 24/7. Goes on an **Oracle
  Cloud VM**.
- **The dashboard** (`dashboard/`, run via `wsgi.py`) — a normal Flask web app. Goes on **Render**
  (free).

They both read/write the same JSON files (`economy.json`, `config.json`, `warnings.json`, etc.),
so if you want the dashboard to see live data, run **both on the same Oracle Cloud VM** instead
(covered in Part 2, step 7) — that's the simpler setup and it's what's recommended if you're
already comfortable with the VM. Part 1 (Render) is there for a proper zero-cost dashboard on its
own domain if you'd rather split them up; the trade-off is Render's free tier spins the site down
after 15 minutes idle, so it takes ~30-50s to wake back up on the first hit after a quiet period.

---

## Part 1 — Hosting the dashboard for free (Render)

Render's free web service tier needs no credit card, supports Flask out of the box, and gives you
a real `https://yourapp.onrender.com` URL with TLS already handled.

### 1. Push the project to GitHub
Render deploys from a git repo.

```
cd gambly-standalone
git init
git add .
git commit -m "initial commit"
```

Create a new (private, if you want) repo on GitHub, then:

```
git remote add origin https://github.com/<you>/<repo>.git
git branch -M main
git push -u origin main
```

Your `.gitignore` already excludes `.env`, so your real token/secrets never get pushed. Good —
keep it that way.

> If you're only hosting the dashboard here and running the bot on Oracle Cloud (Part 2), that's
> fine — this repo can hold both, Render will just run the web half.

### 2. Create the Render web service
1. Go to [render.com](https://render.com) → sign up (GitHub login is easiest) → **New +** →
   **Web Service**.
2. Connect the repo you just pushed.
3. Fill in:
   - **Name**: whatever you want, this becomes part of your URL (`<name>.onrender.com`)
   - **Region**: closest to you
   - **Branch**: `main`
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn -w 2 -b 0.0.0.0:$PORT wsgi:app`
   - **Instance Type**: **Free**

### 3. Add environment variables
On the same setup page (or Settings → Environment after creating), add every variable from your
`.env` **except** you don't need `DISCORD_TOKEN` here if the bot itself isn't running on Render —
the dashboard doesn't need the bot token, only `DISCORD_CLIENT_ID`/`SECRET`. Set at minimum:

```
DISCORD_CLIENT_ID=...
DISCORD_CLIENT_SECRET=...
DISCORD_REDIRECT_URI=https://<your-render-name>.onrender.com/callback
FLASK_SECRET_KEY=...        (generate with: python -c "import secrets; print(secrets.token_hex(32))")
FLASK_ENV=production
BOT_NAME=...
BOT_ADMIN_IDS=...           (optional)
```

### 4. Update the Discord redirect URI
Back in the [Developer Portal](https://discord.com/developers/applications) → your app →
**OAuth2** → **Redirects**, add `https://<your-render-name>.onrender.com/callback` (must match
`DISCORD_REDIRECT_URI` byte-for-byte). Remove/keep `http://localhost:5000/callback` too if you
still want to test locally.

### 5. Deploy
Click **Create Web Service**. Render builds and deploys automatically; every future `git push`
redeploys it. Watch the **Logs** tab for errors on first boot.

### 6. Known free-tier limitation
Render's free instances **spin down after 15 minutes of no traffic** and take 30-50 seconds to
wake up on the next request. Fine for an admin dashboard you check occasionally; if you want it
always warm, either upgrade to Render's cheapest paid tier, or just run the dashboard on the same
Oracle Cloud VM as the bot instead (Part 2, step 7) — that one's always-on for free since it's
your own server.

---

## Part 2 — Deploying the bot to Oracle Cloud

This assumes you already have an Oracle Cloud "Always Free" account and a VM instance running
(Ubuntu is used below — adjust package manager commands if you picked something else). If you
don't have an instance yet: Oracle Cloud Console → **Compute** → **Instances** → **Create
Instance** → pick an **Always Free** eligible shape (the Ampere A1 or VM.Standard.E2.1.Micro) →
Ubuntu image → create/download the SSH key pair it offers you.

### 1. Connect
```
ssh -i /path/to/your-key.pem ubuntu@<your-instance-public-ip>
```

### 2. Install Python and git
```
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 3. Get the code onto the VM
Either clone the repo you pushed in Part 1:
```
git clone https://github.com/<you>/<repo>.git gambly
cd gambly
```
or, if you'd rather not use git, `scp` the zip up and unzip it:
```
# from your own machine:
scp -i /path/to/your-key.pem gambly-standalone.zip ubuntu@<instance-ip>:~
# then on the VM:
unzip gambly-standalone.zip -d gambly && cd gambly
```

### 4. Set up a virtual environment
```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 5. Configure `.env`
```
cp .env.example .env
nano .env
```
Fill in `DISCORD_TOKEN` and `GUILD_ID` at minimum — those two are required. Everything else
(`STAFF_ROLE_ID`, `TICKET_CATEGORY_ID`, and the log channel IDs) is optional: it's only a one-time
default, and you can configure all of it later from the dashboard's **Moderation & Logging** tab
instead of editing `.env` by hand. Save with `Ctrl+O`,
`Enter`, exit with `Ctrl+X`.

### 6. Quick test run
```
python bot.py
```
You should see `<YourBotName> is up` in the console with no errors. `Ctrl+C` to stop once
confirmed — this was just a smoke test, the real run happens as a service below (so it survives
you closing the SSH session and survives reboots).

### 7. Run it as a systemd service (keeps it alive forever)
```
sudo nano /etc/systemd/system/gambly-bot.service
```
Paste in:
```ini
[Unit]
Description=Gambly discord bot
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/gambly
EnvironmentFile=/home/ubuntu/gambly/.env
ExecStart=/home/ubuntu/gambly/venv/bin/python bot.py
Restart=always
RestartSec=5
User=ubuntu

[Install]
WantedBy=multi-user.target
```
(adjust the paths if you cloned somewhere other than `/home/ubuntu/gambly`)

Then:
```
sudo systemctl daemon-reload
sudo systemctl enable --now gambly-bot
sudo systemctl status gambly-bot     # should show "active (running)"
journalctl -u gambly-bot -f          # live logs, Ctrl+C to stop watching
```

The bot now restarts automatically if it crashes, and starts on its own if the VM reboots.

#### Optional — also run the dashboard here instead of Render
If you'd rather have one always-on box instead of splitting bot + dashboard across two platforms,
add a second service for the dashboard on the same VM:
```
sudo nano /etc/systemd/system/gambly-dashboard.service
```
```ini
[Unit]
Description=Gambly web dashboard
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/gambly
EnvironmentFile=/home/ubuntu/gambly/.env
ExecStart=/home/ubuntu/gambly/venv/bin/gunicorn -w 2 -b 0.0.0.0:5000 wsgi:app
Restart=always
User=ubuntu

[Install]
WantedBy=multi-user.target
```
```
sudo systemctl daemon-reload
sudo systemctl enable --now gambly-dashboard
```
Then open port 5000 (or better, put nginx/Caddy in front for TLS on a real domain) — in the
Oracle Cloud Console, edit your instance's **Virtual Cloud Network → Security List** to add an
ingress rule for the port you use, and run `sudo ufw allow 5000` (or your nginx port) on the VM
itself if `ufw` is active. Update `DISCORD_REDIRECT_URI` to point at this VM's address instead of
Render's.

### 8. Updating later
```
cd ~/gambly
git pull                      # or re-upload the zip and overwrite
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart gambly-bot
```

---

## Troubleshooting

**"Unknown Integration" error, or slash commands vanishing/duplicating after a restart**
Two separate things can cause this, and this build now fixes both automatically:
1. Mismatched integration types (guild-install vs. user-install) - fixed by registering
   guild-install only (`default_command_integration_types` in `bot.py`).
2. **Leftover global commands from before this was a single-server build.** If this project was
   ever a multi-server bot before (this one was), it likely did a global command sync at some
   point - global commands and this build's guild-only commands are two *separate* lists as far as
   Discord is concerned, so registering the guild ones does not remove old global ones with the
   same names. Having both active for the same command name is exactly what produces "Unknown
   Integration" and commands that seem to randomly disappear or duplicate. `bot.py` now checks for
   and wipes any stale global commands automatically on every startup - just restart the bot once
   with this build and it clears itself out.

Either way: fully **restart the bot** (a clean resync overwrites Discord's stored command list),
and if Discord's own client still shows stale/duplicate commands for a minute afterward, close and
reopen it - the client caches the command list locally on top of whatever the server sends.

**Bot won't come online / `journalctl -u gambly-bot -f` shows a login error**
Almost always a bad or regenerated `DISCORD_TOKEN`. Re-copy it from the Developer Portal → your
app → **Bot** tab → **Reset Token** (resetting invalidates the old one, so update `.env` right
after).

**Slash commands don't show up in Discord**
Confirm `GUILD_ID` in `.env` matches your actual server ID exactly (Developer Mode on →
right-click server icon → Copy Server ID), then restart the bot. Commands sync to that one guild
on every startup and normally appear within seconds — if they still don't, check the console for a
`FAILED to load cogs....` line, which means a cog didn't load and its commands never got registered.

**Bot joined a server and immediately left again**
That's the single-server lock working as intended — `GUILD_ID` doesn't match that server. Nothing
to fix unless you meant to run it there, in which case update `GUILD_ID`.

**Dashboard shows "Couldn't load roles/channels — check the bot token"** (Moderation & Logging tab)
The dashboard uses `DISCORD_TOKEN` (bot token, not the OAuth2 client secret) to fetch live
roles/channels. Confirm it's set and the bot is actually a member of the server you're viewing.

**A log type is on but nothing shows up in the channel**
Check the bot has **View Channel** and **Send Messages** permission in that specific channel —
category-level permission overrides are a common cause. The bot logs a line to its own console
(`[logging] no permission to send in the mod log channel...`) when this happens, so `journalctl -u
gambly-bot -f` will tell you exactly which channel and why.

**Report / ticket / log channel got deleted**
Nothing crashes — `logging_utils.send_log()` catches a missing channel, logs a console warning, and
the triggering command still completes normally. Just re-pick a channel from the dashboard's
Moderation & Logging tab.

**`config.json` looks like it's missing new fields for an old server**
That's expected and harmless — fields are added lazily the next time that guild's config is
touched (a command runs, or you open it in the dashboard), not retroactively for every server on
disk. Nothing reads a missing field as an error; every lookup has a default.

**Dashboard 500s on `/api/guild/<gid>/...`**
Almost always `FLASK_SECRET_KEY` unset (the app refuses to start without one — check the very
first line of dashboard logs) or `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` not matching the
Developer Portal.

---

## Quick checklist
- [ ] `GUILD_ID` set — confirm the bot only shows up in your one server
- [ ] Logged into the dashboard once, opened the server, and set staff role / ticket category /
      log channels from the **Moderation & Logging** tab (or set their `.env` defaults before
      first boot)
- [ ] `/ticket-panel` posted once in whatever channel you want it in
- [ ] Bot running as a systemd service on Oracle Cloud (survives reboot/crash)
- [ ] Dashboard reachable, either via Render or the same VM
- [ ] `DISCORD_REDIRECT_URI` in `.env` matches the Redirect URL in the Developer Portal exactly
