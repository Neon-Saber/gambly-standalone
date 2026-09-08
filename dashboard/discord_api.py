"""
Thin wrapper around the bits of Discord's HTTP API the dashboard needs:
- OAuth2 code exchange + refresh (acting as the logged-in user)
- fetching that user's identity + guild list
- a few bot-token calls (role list, leaving a guild) used for the
  management features on the guild settings page

Nothing here is cached beyond the Flask session - if you rename a server
in Discord you'll see the new name next time you log back in.
"""
import os
import time
import requests

API_BASE = "https://discord.com/api/v10"

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")

# identify -> who is this person. guilds -> which servers are they in, and
# what's their permission bitfield in each. Neither exposes anything the
# person doesn't already see in their own Discord client.
OAUTH_SCOPES = "identify guilds"

ADMINISTRATOR = 0x8
MANAGE_GUILD = 0x20


def authorize_url(state):
    from urllib.parse import urlencode
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": OAUTH_SCOPES,
        "state": state,
        "prompt": "none",
    }
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


def exchange_code(code):
    """Trade a one-time OAuth code for the user's access/refresh tokens."""
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    r = requests.post(f"{API_BASE}/oauth2/token", data=data, timeout=10)
    r.raise_for_status()
    tok = r.json()
    tok["obtained_at"] = time.time()
    return tok


def refresh_token(refresh_tok):
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
    }
    r = requests.post(f"{API_BASE}/oauth2/token", data=data, timeout=10)
    r.raise_for_status()
    tok = r.json()
    tok["obtained_at"] = time.time()
    return tok


def fetch_user(access_token):
    r = requests.get(f"{API_BASE}/users/@me",
                      headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_user_guilds(access_token):
    """Every server this person is a member of, with their permission
    bitfield in each - NOT filtered to servers the bot is in yet."""
    r = requests.get(f"{API_BASE}/users/@me/guilds",
                      headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
    r.raise_for_status()
    return r.json()


def can_manage(guild_entry):
    """True if this OAuth guild entry (from fetch_user_guilds) has enough
    permission to administer the bot's settings there - server owner,
    Administrator, or Manage Server, same bar Discord itself uses to show
    the server's Settings cog."""
    if guild_entry.get("owner"):
        return True
    try:
        perms = int(guild_entry.get("permissions", 0))
    except (TypeError, ValueError):
        return False
    return bool(perms & ADMINISTRATOR) or bool(perms & MANAGE_GUILD)


def guild_icon_url(guild_id, icon_hash, size=64):
    if not icon_hash:
        return None
    ext = "gif" if str(icon_hash).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size={size}"


def avatar_url(user_id, avatar_hash, discriminator="0", size=64):
    if not avatar_hash:
        idx = (int(user_id) >> 22) % 6 if discriminator == "0" else int(discriminator) % 5
        return f"https://cdn.discordapp.com/embed/avatars/{idx}.png"
    ext = "gif" if str(avatar_hash).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.{ext}?size={size}"


# ---------------- bot-token calls (server-side only, never sent to the browser) ----------------
def _bot_headers():
    return {"Authorization": f"Bot {BOT_TOKEN}"}


_my_guilds_cache = {"guilds": None, "ts": 0}
_MY_GUILDS_TTL = 60  # seconds - avoids hammering Discord on every page load


def bot_fetch_my_guilds(force=False):
    """Every server the bot's own account is *actually* a member of right
    now, straight from Discord (not from our flat files). This is the only
    reliable way to tell "installed" apart from "has data" - a server can
    have economy/config data because someone used a slash command there
    through Discord's per-user app install (no guild membership required)
    without the bot ever having been added to that server, and a server
    can have zero data because it was just added and nobody's played yet.

    Returns {guild_id: {"name":..., "icon": icon_url_or_None}}. Cached for
    a minute since this can page through a lot of servers and every admin
    page load would otherwise re-fetch it.
    """
    now = time.time()
    if not force and _my_guilds_cache["guilds"] is not None and now - _my_guilds_cache["ts"] < _MY_GUILDS_TTL:
        return _my_guilds_cache["guilds"]

    guilds = {}
    after = None
    for _ in range(25):  # 25 * 200 = 5000 servers, hard cap so a bug can't loop forever
        params = {"limit": 200}
        if after:
            params["after"] = after
        r = requests.get(f"{API_BASE}/users/@me/guilds", headers=_bot_headers(), params=params, timeout=10)
        if r.status_code != 200:
            # don't fail silently - an empty dict here makes every single
            # server show up as "Not installed" on the dashboard even when
            # the bot is actually in them. 401 almost always means
            # DISCORD_TOKEN is missing/wrong/stale wherever this is hosted
            # (check it separately per-host - Render env vars are not the
            # same as your local .env file).
            print(f"[bot_fetch_my_guilds] Discord returned {r.status_code}: {r.text[:200]} "
                  f"- check DISCORD_TOKEN is set correctly on this host")
            break
        page = r.json()
        if not page:
            break
        for g in page:
            guilds[g["id"]] = {"name": g.get("name", g["id"]), "icon": guild_icon_url(g["id"], g.get("icon"))}
        if len(page) < 200:
            break
        after = page[-1]["id"]

    _my_guilds_cache["guilds"] = guilds
    _my_guilds_cache["ts"] = now
    return guilds


def bot_fetch_guild(guild_id):
    r = requests.get(f"{API_BASE}/guilds/{guild_id}", headers=_bot_headers(), timeout=10)
    if r.status_code != 200:
        return None
    return r.json()


def bot_fetch_roles(guild_id):
    r = requests.get(f"{API_BASE}/guilds/{guild_id}/roles", headers=_bot_headers(), timeout=10)
    if r.status_code != 200:
        return []
    roles = r.json()
    # drop @everyone and bot-managed integration roles - not useful as a manager role
    return [
        {"id": ro["id"], "name": ro["name"], "color": ro.get("color", 0)}
        for ro in roles
        if ro["name"] != "@everyone" and not ro.get("managed")
    ]


# Discord channel `type` values we care about here - see Discord's docs for
# the full list, most of the rest (voice, stage, forum, etc.) aren't useful
# as a log/ticket destination.
CHANNEL_TYPE_TEXT = 0
CHANNEL_TYPE_CATEGORY = 4
CHANNEL_TYPE_ANNOUNCEMENT = 5


def bot_fetch_channels(guild_id):
    """Text channels and categories for this guild, straight from Discord.
    Used to populate the log-channel / ticket-category dropdowns on the
    dashboard's Moderation & Logging tab."""
    r = requests.get(f"{API_BASE}/guilds/{guild_id}/channels", headers=_bot_headers(), timeout=10)
    if r.status_code != 200:
        return {"text": [], "categories": []}
    channels = r.json()
    text = [{"id": c["id"], "name": c["name"], "parent_id": c.get("parent_id")}
            for c in channels if c.get("type") in (CHANNEL_TYPE_TEXT, CHANNEL_TYPE_ANNOUNCEMENT)]
    categories = [{"id": c["id"], "name": c["name"]}
                  for c in channels if c.get("type") == CHANNEL_TYPE_CATEGORY]
    text.sort(key=lambda c: c["name"].lower())
    categories.sort(key=lambda c: c["name"].lower())
    return {"text": text, "categories": categories}


def bot_leave_guild(guild_id):
    r = requests.delete(f"{API_BASE}/users/@me/guilds/{guild_id}", headers=_bot_headers(), timeout=10)
    return r.status_code in (204, 200)
