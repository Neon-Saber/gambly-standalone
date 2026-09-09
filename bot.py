import json, os, random, time, asyncio
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # must run before any of our own modules import (store.py reads
                # UPSTASH_REDIS_REST_URL/TOKEN from the environment at import
                # time, so this has to happen first or it never sees .env)

import discord
from discord.ext import commands, tasks
from discord import Option

import config_schema as cfgschema
import logging_utils
import embeds
import cog_utils as cu
import store
from cogs.moderation import load_warnings, save_warnings

TOKEN = os.getenv("DISCORD_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")

# ---------------- single-server lock ----------------
# this build is locked to one server. set GUILD_ID in .env to your server's
# ID (right-click the server icon in Discord with Developer Mode on ->
# "Copy Server ID"). slash commands sync ONLY to this guild (near-instant,
# instead of the up-to-an-hour wait for global commands), and on_ready will
# make the bot leave any other server it somehow ends up in.
#
# everything else that used to be read straight from the environment here
# (STAFF_ROLE_ID, MOD_LOG_CHANNEL_ID, TICKET_CATEGORY_ID, etc.) now lives in
# config.json instead, via config_schema.py / logging_utils.py - the .env
# values for those still work, but only as the one-time DEFAULT the first
# time this guild's config gets created. after that the dashboard's
# Moderation & Logging tab is the source of truth, live, no restart needed.
GUILD_ID = os.getenv("GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID) if GUILD_ID.isdigit() else None

# economy numbers - been tweaking these since testing on my server, ppl kept
# hitting the daily too easy so bumped the cooldown back up
starting_bal = 1000
daily_amt = 250
DAILY_WAIT = 86400
WORK_WAIT = 3600
ROB_WAIT = 3600 * 3
CRIME_WAIT = 2700  # 45m - riskier than /work so shorter isn't fair, keeping it close
BEG_WAIT = 300  # 5m - tiny amount so no real reason to gate it hard
econ_file = Path(__file__).parent / "economy.json"
# separate ledger for anywhere there's no server attached - straight DMs,
# group DMs, and (via user-install) servers Discord won't hand us guild-level
# member data for. one shared "personal" bucket, keyed by user id, so your
# personal balance follows you between every DM/group chat instead of each
# one starting fresh.
personal_econ_file = Path(__file__).parent / "personal_economy.json"
cfg_file = Path(__file__).parent / "config.json"  # kept for reference; config_schema.py (CFG_FILE) is now the actual source of truth
log_file = Path(__file__).parent / "activity.json"
settings_file = Path(__file__).parent / "settings.json"
history_file = Path(__file__).parent / "balance_history.json"

# loan/shop numbers
LOAN_MAX = starting_bal * 2
LOAN_INTEREST = 0.20
LOAN_TERM = 86400  # 24h to pay it back before the house comes collecting
BANK_INTEREST = 0.02  # small daily interest on whatever's sitting in the bank
SHOP = {
    "shield": {"price": 300, "desc": "blocks /rob against you for 24h per use"},
    "charm": {"price": 200, "desc": "doubles your next /daily per use"},
    "insurance": {"price": 400, "desc": "blocks the next successful /rob against you, one use"},
    "vault": {"price": 0, "desc": "craft from 3 shields - 48h rob block + half loan interest (can't be bought directly)"},
    "lockpick": {"price": 250, "desc": "guarantees your next /rob succeeds (a shield on the target still blocks it)"},
    "energy_drink": {"price": 150, "desc": "instantly resets your /work and /crime cooldowns"},
}

# permanent one-time upgrades - bought separately from the consumable SHOP
# above since they're never used up or pawned, just owned forever once bought
UPGRADES = {
    "briefcase": {"price": 800, "desc": "+25% chips from every /work, forever"},
    "resume": {"price": 1200, "desc": "cuts your /work cooldown by 15 minutes, forever"},
}

# passive-income businesses - buy a tier, income builds up hourly whether
# you're online or not, claim it anytime with /collect. buying a higher tier
# fully replaces (and auto-collects) whatever you had before.
BUSINESS_TIERS = {
    "stand": {"price": 500, "hourly": 15, "label": "Lemonade Stand"},
    "shop": {"price": 2500, "hourly": 60, "label": "Chip Shop"},
    "casino": {"price": 10000, "hourly": 300, "label": "Mini Casino"},
}
BUSINESS_ORDER = ["stand", "shop", "casino"]
BUSINESS_CAP_HOURS = 24  # income stops piling up after this long uncollected

GAME_COMMANDS = {"coinflip", "slots", "dice", "roulette", "blackjack", "allin", "war", "hilo", "crash", "mines",
                  "plinko", "horserace", "poker", "wheel", "keno", "baccarat", "rps", "ladder"}
HOLIDAY_SYMS = {"🎄": 40, "🎁": 25, "⛄": 15, "🦌": 10, "🔔": 5, "⭐": 2}


# ---------------- runtime-tunable settings, editable from the admin panel ----------------
# these are the knobs that used to be hardcoded constants above. the constants
# still exist as fallback defaults so the bot works fine with no settings.json
# at all, but the admin panel can override them without touching code/restarting
def loadSettings():
    return store.load(settings_file)


def get_setting(key, default):
    return loadSettings().get(key, default)


# ---------------- lightweight in-memory anti-spam guard ----------------
# not persisted on purpose - just needs to survive between two clicks a
# second apart, resets are fine on bot restart
last_action = {}


def spam_check(member):
    now = time.time()
    last = last_action.get(member.id, 0)
    last_action[member.id] = now
    return (now - last) >= 1.5


def pop_rig(guild, user):
    # dev/testing aid only: when the server owner turns testmode on with
    # ?testmode, THEIR OWN bets always win so payout math/embeds/balance
    # updates can be checked quickly. everyone else's games (including other
    # admins) are always fair random - this never touches other players'
    # outcomes. listed in ?help, logged on toggle so it's never silent.
    if not guild:
        return None
    gcfg = get_guild_cfg(loadCfg(), guild)
    if gcfg.get("testmode") and is_owner(user, guild):
        return "win"
    return None


# ---------------- server tax + weekly tournament stat tracking ----------------
def take_tax(guild, delta):
    """positive delta only - skims tax_pct into the server pot, returns what's left"""
    if delta <= 0:
        return delta
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, guild)
    pct = g.get("tax_pct", 0)
    if pct <= 0:
        return delta
    tax = int(delta * pct / 100)
    if tax > 0:
        g["server_pot"] = g.get("server_pot", 0) + tax
        saveCfg(all_cfg)
    return delta - tax


def record_game(guild, member, net):
    # feeds both the weekly tournament (biggest single win) and the activity
    # count (most active gambler) - checked/reset by the background loop
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, guild)
    wk = g.setdefault("week", {"period_start": time.time(), "biggest_win": None, "activity": {}})
    uid = str(member.id)
    wk["activity"][uid] = wk["activity"].get(uid, 0) + 1
    if net > 0 and (wk["biggest_win"] is None or net > wk["biggest_win"]["amount"]):
        wk["biggest_win"] = {"user_id": uid, "name": member.display_name, "amount": net}
    saveCfg(all_cfg)


def award(a, key, label, guild_name=None):
    badges = a.setdefault("badges", [])
    if key not in badges:
        badges.append(key)
        if guild_name:
            log_event(guild_name, f"{a['name']} unlocked the '{label}' badge")


def check_bet_badges(a, guild_name, bet_amount, bal_before, net):
    # called after a game resolves - bal_before is the balance BEFORE the bet
    # was placed, net is win/loss amount (positive = won, negative = lost)
    if bet_amount >= 5000:
        award(a, "whale", "Whale (5k+ single bet)", guild_name)
    if bal_before <= 50 and a["bal"] >= 2000:
        award(a, "comeback_kid", "Comeback Kid", guild_name)
    if net > 0:
        a["win_streak"] = a.get("win_streak", 0) + 1
        if a["win_streak"] >= 5:
            award(a, "win_streak", "On Fire (5-win streak)", guild_name)
    else:
        a["win_streak"] = 0

debug = False

# need message_content for the prefix commands to actually read what people type,
# and members so @mentions/name lookups resolve properly. gotta turn both of these
# on in the dev portal under Bot > Privileged Gateway Intents or none of this works
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


def get_prefix(bot_, message):
    if not message.guild:
        return "!"
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, message.guild)
    return g.get("prefix", "!")


bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    # single-server build: register slash commands to just this guild so
    # they show up within seconds instead of the up-to-an-hour global sync.
    # falls back to a global sync if GUILD_ID isn't set yet.
    debug_guilds=[GUILD_ID] if GUILD_ID else None,
    # guild-install ONLY. the original multi-server version of this bot also
    # allowed user-install (Discord's "Apps" tab -> add to your own account,
    # then use its slash commands anywhere, including servers the bot was
    # never added to, and DMs). that's incompatible with the single-server
    # lock this build adds - a user-installed command bypasses the lock
    # completely, since it doesn't require the bot to be a member of
    # anywhere - and mixing the two integration types is also what was
    # causing the "Unknown Integration" errors and commands vanishing after
    # sync. guild-install matches how every command here actually expects
    # to run (ctx.guild, moderation permissions, per-guild config, etc.).
    default_command_integration_types={discord.IntegrationType.guild_install},
)


# ---------------- per-game channel locking (global check on every command) ----------------
class GameChannelLocked(commands.CheckFailure):
    """Raised (instead of a plain False) so on_application_command_error
    can recognize this specific case and not pile a generic "something
    broke" message on top of the "play that in #channel instead" message
    the check below already sent."""
    pass


async def _game_channel_check(ctx):
    cmd_name = ctx.command.name if ctx.command else None
    if not cmd_name or cmd_name not in cu.GAME_CHANNEL_ALIASES:
        return True  # not a channel-locked game - never blocked
    if not ctx.guild:
        return True  # DMs/group chats have no channels to lock to

    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    channel_id, changed = cu.resolve_game_channel(ctx.guild, g, cmd_name)
    if changed:
        saveCfg(all_cfg)

    if channel_id is None or ctx.channel.id == channel_id:
        return True

    await cu.respond(ctx, f"play that in <#{channel_id}> instead", ephemeral=True)
    raise GameChannelLocked()


bot.add_check(_game_channel_check)


# ---------------- "space" abstraction: server vs. personal (DM/group chat) ----------------
# every economy/config function below (ensure_guild, acct, get_guild_cfg, log_event,
# take_tax, record_game, is_banned, is_manager, is_owner, pop_rig...) only ever touches
# .id and .name on whatever "guild-shaped" thing you hand it. that's on purpose - it
# means a real discord.Guild AND this little stand-in both work everywhere those
# functions get called, with zero changes to the functions themselves.
#
# when there's no real guild (a DM, a group DM, or a user-installed slash command run
# somewhere Discord won't give us guild data), everyone gets bucketed into this single
# shared "personal" space instead. balances there live in personal_economy.json,
# completely separate from any server's economy.json, and follow you across every DM.
class PersonalSpace:
    id = "dm"
    name = "Direct Messages"
    owner_id = None  # nobody "owns" the DM space, so is_owner() always says no

    def get_member(self, user_id):
        return None  # no member cache outside a real guild - callers fall back to stored names


PERSONAL_SPACE = PersonalSpace()


def get_space(ctx):
    """the guild-or-personal-space for wherever this command was run"""
    return ctx.guild if ctx.guild else PERSONAL_SPACE


def is_real_guild(space):
    return isinstance(space, discord.Guild)


def econ_file_for(space):
    return personal_econ_file if not is_real_guild(space) else econ_file


# ---------------- economy save/load ----------------
# balances are now split per-server, economy.json looks like:
# { "<guild id>": { "name": "cool server", "users": { "<user id>": {...} } } }
# storing the guild/user names alongside the numbers so the admin panel can
# show real names without needing its own discord connection
def loadEcon(file=None):
    # file defaults to the server ledger so every pre-existing call site that
    # doesn't pass one (guild-only commands) behaves exactly like before
    file = file or econ_file
    return store.load(file)


def save(econ, file=None):
    file = file or econ_file
    store.save(file, econ)


def ensure_guild(econ, guild):
    gid = str(guild.id)
    if gid not in econ:
        econ[gid] = {"name": guild.name, "users": {}}
    else:
        econ[gid]["name"] = guild.name  # keep it fresh if the server gets renamed
    return econ[gid]


def new_acct(name):
    return {
        "name": name, "bal": get_setting("starting_bal", starting_bal), "bank": 0,
        "last_daily": 0, "last_work": 0, "last_rob": 0, "last_crime": 0, "last_beg": 0,
        "loan": None, "shield_until": 0, "charm": False, "insurance": False, "lockpick": False,
        "inv": {"shield": 0, "charm": 0, "vault": 0, "insurance": 0, "lockpick": 0, "energy_drink": 0},
        "vault_interest_until": 0,
        "investments": [],
        "badges": [],
        "daily_streak": 0,
        "win_streak": 0,
        "business": None,
        "upgrades": {},
    }


def check_loan(u, guild_name):
    # runs every time an account is touched. two jobs:
    # 1) if a loan just went past due, the house helps itself to whatever's
    #    sitting in wallet+bank
    # 2) if it's ALREADY defaulted, the remaining debt compounds 5%/day til
    #    it's paid off - staying defaulted isn't a free pass
    loan = u.get("loan")
    if not loan:
        return

    if loan.get("defaulted"):
        last_grow = loan.get("last_grow", loan.get("due", time.time()))
        days = int((time.time() - last_grow) // 86400)
        if days >= 1:
            grown = int(loan["owed"] * (1.05 ** days))
            if grown > loan["owed"]:
                loan["owed"] = grown
                loan["last_grow"] = last_grow + days * 86400
                log_event(guild_name, f"{u['name']}'s defaulted debt grew to {chips(loan['owed'])}")
        return

    if time.time() < loan["due"]:
        return
    owed = loan["owed"]
    avail = u["bal"] + u["bank"]
    if avail >= owed:
        from_bal = min(u["bal"], owed)
        u["bal"] -= from_bal
        u["bank"] -= (owed - from_bal)
        u["loan"] = None
        log_event(guild_name, f"{u['name']}'s overdue loan ({chips(owed)}) was auto-repaid from their balance/bank")
    else:
        u["bal"] = 0
        u["bank"] = 0
        loan["owed"] = owed - avail
        loan["defaulted"] = True
        loan["last_grow"] = time.time()
        award(u, "survived_default", "Survived a Default", guild_name)
        log_event(guild_name, f"{u['name']} defaulted on a loan, {chips(loan['owed'])} still owed and will keep growing til it's paid - can still play games to try to earn it back")


def check_investments(u, guild_name):
    # matured investments pay out automatically the next time the account's
    # touched, no need for a separate /collect command
    still_locked = []
    for inv in u.get("investments", []):
        if time.time() >= inv["matures"]:
            u["bal"] += inv["payout"]
            log_event(guild_name, f"{u['name']}'s investment matured, +{chips(inv['payout'])}")
        else:
            still_locked.append(inv)
    u["investments"] = still_locked


def acct(econ, guild, member):
    g = ensure_guild(econ, guild)
    uid = str(member.id)
    if uid not in g["users"]:
        g["users"][uid] = new_acct(member.display_name)
    else:
        u = g["users"][uid]
        u["name"] = member.display_name  # refresh in case they changed nick
        u.setdefault("last_work", 0)
        u.setdefault("last_rob", 0)
        u.setdefault("last_crime", 0)
        u.setdefault("last_beg", 0)
        u.setdefault("bank", 0)
        u.setdefault("loan", None)
        u.setdefault("shield_until", 0)
        u.setdefault("charm", False)
        u.setdefault("insurance", False)
        u.setdefault("lockpick", False)
        u.setdefault("inv", {"shield": 0, "charm": 0, "vault": 0, "insurance": 0, "lockpick": 0, "energy_drink": 0})
        u["inv"].setdefault("insurance", 0)
        u["inv"].setdefault("lockpick", 0)
        u["inv"].setdefault("energy_drink", 0)
        u.setdefault("vault_interest_until", 0)
        u.setdefault("investments", [])
        u.setdefault("badges", [])
        u.setdefault("business", None)
        u.setdefault("upgrades", {})
        u.setdefault("daily_streak", 0)
        u.setdefault("win_streak", 0)
        check_loan(u, g["name"])
        check_investments(u, g["name"])
        if u["bal"] + u["bank"] >= 10000:
            award(u, "high_roller", "High Roller", g["name"])
    return g["users"][uid]


def chips(n):
    return f"🪙 {n:,} chips"


# ---------------- activity log, mostly for the admin panel ----------------
# not every coinflip, just the stuff worth an admin actually seeing:
# loans, shop buys, bans, manager changes, resets
def log_event(guild_name, text):
    entries = store.load(log_file, default=[])
    entries.append({"ts": time.time(), "guild": guild_name, "text": text})
    entries = entries[-500:]  # keep it from growing forever
    store.save(log_file, entries)


# ---------------- per-server config (prefix, managers, bans) ----------------
def loadCfg():
    return cfgschema.load_cfg()


def saveCfg(c):
    cfgschema.save_cfg(c)


def get_guild_cfg(all_cfg, guild):
    # `guild` is either a real discord.Guild or the PersonalSpace stand-in
    # (see PersonalSpace above) - both have .id and .name, which is all
    # ensure_guild() needs. centralized in config_schema.py now so the bot
    # and the dashboard can't drift out of sync on what fields exist/default to.
    return cfgschema.ensure_guild(all_cfg, guild.id, guild.name)


def is_manager(member, guild):
    # admins are always managers, no need to set anything up for that
    if member.guild_permissions.administrator:
        return True
    g = get_guild_cfg(loadCfg(), guild)
    if member.id in g["managers"]:
        return True
    if g["manager_role"] and any(r.id == g["manager_role"] for r in member.roles):
        return True
    return False


def is_owner(member, guild):
    return guild.owner_id == member.id


def is_banned(member, space):
    # only an explicit manager-issued ban locks someone out of the games.
    # defaulting on a loan used to also trigger this automatically, but
    # that meant a bad bet could wipe someone's wallet AND lock them out
    # of every game with no way to earn their way back out except grinding
    # /work, /crime, /beg - unfair for something that isn't rule-breaking.
    # the debt still compounds daily in check_loan() until it's paid off,
    # that's punishment enough; being in debt no longer bans you from playing.
    g = get_guild_cfg(loadCfg(), space)
    return member.id in g["banned"]


# helper so i dont have to write ctx.respond vs ctx.send branches everywhere,
# slash commands use ApplicationContext (.respond), prefix commands use
# regular Context (.send). both take content/embed/view so this just picks the right one
async def reply(ctx, content=None, embed=None, view=None, ephemeral=False):
    if hasattr(ctx, "respond"):
        await ctx.respond(content=content, embed=embed, view=view, ephemeral=ephemeral)
    else:
        await ctx.send(content=content, embed=embed, view=view)


def need_guild(ctx):
    # most economy commands now work fine outside a server (see get_space /
    # PersonalSpace above) - this is only for the genuinely server-only stuff:
    # multiplayer commands (give/rob/duel/bounty) that need another real
    # member, guild-wide views (leaderboard/debtors/bounties), and admin/
    # management commands (setprefix, banuser, setmanager, etc.)
    return ctx.guild is not None


@tasks.loop(hours=1)
async def weekly_tournament_check():
    # runs hourly, only actually pays out once 7 days have passed since a
    # server's "week" tracking period started. resets the tracker after.
    all_cfg = loadCfg()
    changed = False
    for gid, g in list(all_cfg.items()):
        if gid == PERSONAL_SPACE.id:
            continue  # no weekly tournament for the personal/DM space, it's solo
        wk = g.get("week")
        if not wk or time.time() - wk.get("period_start", time.time()) < 7 * 86400:
            continue
        guild = bot.get_guild(int(gid))
        if not guild:
            continue
        econ = loadEcon()
        payouts = []
        biggest = wk.get("biggest_win")
        if biggest:
            member = guild.get_member(int(biggest["user_id"]))
            if member:
                a = acct(econ, guild, member)
                prize = 1000
                a["bal"] += prize
                payouts.append(f"🏆 biggest win of the week: {member.display_name} (+{chips(prize)})")
        activity = wk.get("activity", {})
        if activity:
            top_uid = max(activity, key=activity.get)
            member = guild.get_member(int(top_uid))
            if member:
                a = acct(econ, guild, member)
                prize = 500
                a["bal"] += prize
                payouts.append(f"🎰 most active gambler: {member.display_name} (+{chips(prize)})")
        save(econ)
        g["week"] = {"period_start": time.time(), "biggest_win": None, "activity": {}}
        changed = True
        channel_id = g.get("announce_channel")
        if channel_id and payouts:
            channel = guild.get_channel(channel_id)
            if channel:
                try:
                    await channel.send("**weekly casino tournament results**\n" + "\n".join(payouts))
                except discord.Forbidden:
                    pass
    if changed:
        saveCfg(all_cfg)


@weekly_tournament_check.before_loop
async def before_weekly_tournament_check():
    await bot.wait_until_ready()


import hashlib

_CMD_HASH_FILE = Path(__file__).parent / ".command_hash.json"


def _command_hashes():
    """{command name: fingerprint of that command's name/description/
    options} for every currently-registered local command. Per-command on
    purpose (not one hash for the whole set) - that's what lets on_connect
    below tell exactly WHICH commands changed instead of just "something
    changed"."""
    hashes = {}
    for c in bot.pending_application_commands:
        blob = json.dumps(c.to_dict(), sort_keys=True, default=str)
        hashes[c.name] = hashlib.sha256(blob.encode()).hexdigest()
    return hashes


def _load_cached_hashes():
    if not _CMD_HASH_FILE.exists():
        return {}
    try:
        cached = json.loads(_CMD_HASH_FILE.read_text())
    except Exception:
        return {}
    # old format from before per-command hashing was {"hash": "..."} - on
    # the first boot after upgrading this just looks like "everything is
    # new", which is harmless: Discord treats an upsert of a name that
    # already exists as an edit, not a create, so it doesn't cost anything
    # from the 200/day creates budget. Every boot after that is properly
    # incremental once the new-format file is written below.
    return cached.get("commands", {})


def _save_cached_hashes(hashes):
    _CMD_HASH_FILE.write_text(json.dumps({"commands": hashes}))


async def _sync_commands_incrementally(new_hashes, old_hashes):
    """Only touches commands that actually changed since the last
    successful sync - added, edited, or removed - instead of bulk-
    overwriting the entire ~80-command set every time ANYTHING changes.
    That full-bulk-every-time behavior is what previously burned through
    Discord's 200-guild-command-CREATES/day cap (error 30034) from just a
    few restarts while testing.

    Edits and deletes don't count against that cap at all - only genuinely
    new command names do (a Discord-side rule, nothing this bot controls),
    so in practice this only ever spends budget on commands that are
    truly new, never on ones that just got tweaked or removed.
    """
    to_create = [name for name in new_hashes if name not in old_hashes]
    to_delete = [name for name in old_hashes if name not in new_hashes]
    to_edit = [name for name in new_hashes
               if name in old_hashes and new_hashes[name] != old_hashes[name]]

    if not (to_create or to_delete or to_edit):
        return  # only unrelated commands' names churned in old_hashes (e.g. a stale entry) - nothing to actually do

    # one GET to map name -> Discord's command ID, needed for edits/deletes
    # (creates don't need an existing ID to target). GET calls are free,
    # they don't count against anything.
    remote = await bot.http.get_guild_commands(bot.application_id, GUILD_ID)
    remote_ids = {c["name"]: c["id"] for c in remote}
    local_by_name = {c.name: c for c in bot.pending_application_commands}

    for name in to_create:
        await bot.http.upsert_guild_command(bot.application_id, GUILD_ID, local_by_name[name].to_dict())
    for name in to_edit:
        remote_id = remote_ids.get(name)
        if remote_id is None:
            # shouldn't happen (it was in old_hashes, so Discord should
            # already know about it) but don't let one weird case take
            # down the rest of the sync - upsert-by-name still works
            await bot.http.upsert_guild_command(bot.application_id, GUILD_ID, local_by_name[name].to_dict())
        else:
            await bot.http.edit_guild_command(bot.application_id, GUILD_ID, int(remote_id), local_by_name[name].to_dict())
    for name in to_delete:
        remote_id = remote_ids.get(name)
        if remote_id is not None:
            await bot.http.delete_guild_command(bot.application_id, GUILD_ID, int(remote_id))

    untouched = len(new_hashes) - len(to_create) - len(to_edit)
    print(f"synced commands: {len(to_create)} created, {len(to_edit)} edited, "
          f"{len(to_delete)} deleted, {untouched} untouched")


@bot.event
async def on_connect():
    # py-cord normally does this silently as part of its default on_connect
    # handler - overriding it just to get visible startup feedback, since
    # command syncing is the step most likely to hang/fail (Discord rate
    # limits, bad token permissions, etc.) and "nothing printed for 30
    # seconds" otherwise looks identical to "frozen".
    #
    # IMPORTANT: on_connect fires on every reconnect, not just the first
    # startup, so this needs to be cheap and safe to run repeatedly. Skip
    # entirely if nothing changed; otherwise sync only what did (see
    # _sync_commands_incrementally above).
    new_hashes = _command_hashes()
    old_hashes = _load_cached_hashes()

    if new_hashes == old_hashes:
        print(f"commands unchanged ({len(new_hashes)}) - skipping sync")
        return

    if not bot.auto_sync_commands:
        _save_cached_hashes(new_hashes)
        return

    if GUILD_ID:
        print(f"command set changed - syncing only what's different, to guild {GUILD_ID}...")
        try:
            await _sync_commands_incrementally(new_hashes, old_hashes)
        except Exception as e:
            print(f"incremental command sync failed ({e}) - falling back to a full sync this one time")
            await bot.sync_commands()
    else:
        # no single guild to target an incremental diff at, and global
        # commands propagate over up to an hour anyway regardless of sync
        # method - bulk is fine here, this path is only hit if GUILD_ID
        # isn't set, which this project's single-server design discourages.
        print("connected - syncing commands globally (GUILD_ID not set)...")
        await bot.sync_commands()

    _save_cached_hashes(new_hashes)


@bot.event
async def on_ready():
    print(f"{bot.user} is up")
    if debug:
        print("debug on")

    # wipe any GLOBAL slash commands left over from this application's past
    # (this project started life as a multi-server bot, which very likely
    # did at least one global sync before GUILD_ID/debug_guilds existed).
    # global commands and this build's guild-only commands are two separate
    # lists as far as Discord is concerned - registering the guild ones
    # does NOT remove old global ones with the same names, and having both
    # active at once is exactly what causes "Unknown Integration" errors
    # and commands seeming to randomly vanish/duplicate. safe to run every
    # startup - if there's nothing global left, this is a single harmless
    # no-op API call.
    try:
        existing_global = await bot.http.get_global_commands(bot.application_id)
        if existing_global:
            await bot.http.bulk_upsert_global_commands(bot.application_id, [])
            print(f"cleared {len(existing_global)} stale global slash command(s) left over from "
                  f"before this became a guild-only bot")
    except Exception as e:
        # never let this non-critical cleanup step take the bot down
        print(f"couldn't check/clear global commands (non-fatal, will retry next restart): {e}")

    # single-server lock: bail out of anywhere that isn't the configured
    # server. covers someone accepting an invite link that got shared/leaked,
    # or the bot being re-added somewhere by mistake.
    if GUILD_ID:
        for g in list(bot.guilds):
            if g.id != GUILD_ID:
                print(f"not configured for '{g.name}' ({g.id}) - leaving")
                try:
                    await g.leave()
                except discord.HTTPException as e:
                    print(f"couldn't leave {g.id}: {e}")
    else:
        print("WARNING: GUILD_ID isn't set in .env - the bot will accept any server. "
              "Set GUILD_ID once you know your server's ID and restart.")

    if not weekly_tournament_check.is_running():
        weekly_tournament_check.start()
    if not lottery_draw_loop.is_running():
        lottery_draw_loop.start()


# ================= COGS (moderation + tickets + reports + message logs) =================
# kept out of this file on purpose - this file is already huge, and the
# gambling logic above shouldn't have to be scrolled past to find/edit the
# mod, ticket, report, or logging commands.
for _ext in ("cogs.moderation", "cogs.tickets", "cogs.reports", "cogs.logging_events", "cogs.leveling", "cogs.welcome", "cogs.server_stats"):
    try:
        bot.load_extension(_ext)
        print(f"loaded {_ext}")
    except Exception as e:
        print(f"FAILED to load {_ext}: {e}")


# ================= HELP =================
HELP_TEXT = (
    "**works in DMs & group chats too**\n"
    "solo stuff (balance, daily/work/crime/beg, bank, loans, shop, business, upgrades, and every "
    "single-player game) works anywhere - your DM balance is separate from any server's and follows "
    "you between DMs. anything needing another real player or a server-wide view (give, rob, duel, "
    "bounty, leaderboard, lottery, manager tools) is server-only. slash commands also work in servers "
    "this bot was never added to, if you've installed it as an app - prefix (!) commands only work in "
    "servers the bot is actually a member of.\n\n"
    "**economy**\n"
    "balance [user], daily (streak bonus up to +45%), work, crime (riskier, bigger payout), "
    "beg (tiny, no risk), give <user> <amt>, rob <user>, "
    "bounty <user> <amt>, bounties, leaderboard, stats [user]\n\n"
    "**bank & debt**\n"
    "deposit <amt>, withdraw <amt> (bank chips are safe from /rob and earn interest)\n"
    "loan <amt> (20% interest, 24h to pay back, defaulted debt grows 5%/day), repay <amt>, debtors\n\n"
    "**passive income & upgrades**\n"
    "business, buybusiness <tier>, collect - own a business, income builds up hourly, claim anytime\n"
    "upgrades, buyupgrade <name> - permanent one-time boosts (bigger /work payouts, shorter cooldown)\n\n"
    "**shop**\n"
    "shop, buy <item>, pawn <item> (sell back for 50%) - shield blocks rob, charm doubles daily, "
    "insurance blocks the next successful rob against you, lockpick guarantees your next rob, "
    "energy_drink resets /work and /crime cooldowns\n\n"
    "**games**\n"
    "coinflip <amt> <heads/tails>, slots <amt> (feeds a growing jackpot), jackpot, dice <amt> <1-6>, "
    "roulette <amt> <red/black/green/number>, blackjack <amt> (Hit/Stand/Double Down), allin <red/black>, "
    "war <amt>, hilo <amt>, crash <amt> <target multiplier>, mines <amt> [mine count], plinko <amt>, "
    "horserace <amt> <horse>, scratch, duel <user> <amt>, wheel <amt>, keno <amt> <numbers>, "
    "baccarat <amt> <player/banker/tie>, rps <amt> <rock/paper/scissors>, ladder <amt> <rungs 1-6>\n\n"
    "**lottery**\n"
    "lottery (see pot/tickets), buyticket <count> - daily drawing, one winner takes the whole pot\n\n"
    "**casino manager stuff** (admins + whoever's set as manager)\n"
    "addchips <user> <amt>, removechips <user> <amt>, banuser <user>, unbanuser <user>, resetuser [user], "
    "forgive <user>, setprefix <prefix>, setannounce <channel>\n\n"
    "**server owner only**\n"
    "setmanager (pick users/roles or auto-create a 'Casino Staff' role), !testmode (prefix-only, your own bets always win, "
    "everyone else's games stay normal random, and every toggle is logged)\n\n"
    "**moderation** (staff role, set in the dashboard, or admins)\n"
    "kick <member> [reason], ban <user> [reason] [delete_days], unban <user id>, "
    "mute <member> <duration> [reason] (timeout, max 28d), unmute <member>, "
    "warn <member> <reason>, warnings <member>, clearwarnings <member> (manage server), "
    "purge <amount> [member] (bulk-delete, max 100), slowmode <duration|off> (max 6h), "
    "lock [reason] / unlock (this channel)\n"
    "**durations** everywhere above accept a bare number (default unit varies by command), short units "
    "(10s, 5m, 2h, 3d, 1w, 2mo, 1y), full words (10 seconds, 5 minutes, 2 hours, 3 days, 1 week, 2 months, "
    "1 year), or combos like 1h30m / '1 week 2 days'\n\n"
    "**tickets & reports**\n"
    "ticket-panel (staff - posts the 'open a ticket' panel in this channel), close-ticket (run inside an open "
    "ticket), add-to-ticket <user> (staff), report <member> <reason> (sends straight to the staff report log)\n\n"
    "**leveling**\n"
    "rank [user] - your level, xp progress, and rank; levels - top 10 leaderboard in this server. earned "
    "automatically by chatting (small cooldown between xp gains so spamming doesn't help). level-up "
    "announcements, the channel it posts in, the message, and which roles unlock at which level are all set "
    "in the dashboard's Leveling & Welcome tab\n\n"
    "**welcome messages**\n"
    "posted automatically when someone joins - channel and message template are set in the dashboard's "
    "Leveling & Welcome tab (no command for this, it's just on/off + configured there)\n\n"
    "**member count**\n"
    "a voice channel can auto-rename itself to show the live non-bot member count (e.g. 'Members: 42') - "
    "toggle, channel, and name template are in the dashboard's Leveling & Welcome tab. updates on join/leave "
    "(rate-limit permitting) and every 10 minutes regardless\n\n"
    "**your data**\n"
    "deletemydata - permanently wipes your own casino account and warning history here, no staff needed\n\n"
    "**per-game channels**\n"
    "each casino game listed above under 'games' can be locked to one channel per server - auto-detected by "
    "matching a channel's name (e.g. #slots), or set explicitly from the dashboard's Game Channels tab, which "
    "also has a Custom Env box for per-server overrides. ask a manager/admin if a game says to play it elsewhere"
)


@bot.slash_command(name="help", description="list of everything this bot can do")
async def help_slash(ctx):
    await reply(ctx, embed=discord.Embed(title="commands", description=HELP_TEXT, color=discord.Color.blurple()))


@bot.command(name="help")
async def help_cmd(ctx):
    await reply(ctx, embed=discord.Embed(title="commands", description=HELP_TEXT, color=discord.Color.blurple()))


# ================= ECONOMY =================
async def do_balance(ctx, who):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, who)
    save(econ, econ_file_for(space))
    desc = chips(a["bal"])
    if a.get("bank"):
        desc += f"\nbank: {chips(a['bank'])}"
    loan = a.get("loan")
    if loan:
        tag = "⚠️ DEFAULTED, " if loan.get("defaulted") else ""
        desc += f"\n{tag}owes: {chips(loan['owed'])}"
    e = discord.Embed(title=who.display_name, description=desc, color=discord.Color.gold())
    e.set_thumbnail(url=who.display_avatar.url)
    await reply(ctx, embed=e)


@bot.slash_command(name="balance", description="check your chips")
async def balance(ctx, user: Option(discord.Member, "who", required=False) = None):
    await do_balance(ctx, user or ctx.author)


@bot.command(name="balance", aliases=["bal"])
async def balance_cmd(ctx, user: discord.Member = None):
    await do_balance(ctx, user or ctx.author)


async def do_daily(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    left = DAILY_WAIT - (time.time() - a["last_daily"])
    if left > 0:
        h, m = int(left / 3600), int((left % 3600) / 60)
        await reply(ctx, content=f"already got it today, {h}h {m}m left", ephemeral=True)
        return
    amt = get_setting("daily_amt", daily_amt)
    now = time.time()
    if a.get("last_daily", 0) > 0 and (now - a["last_daily"]) <= DAILY_WAIT * 2:
        a["daily_streak"] = a.get("daily_streak", 0) + 1
    else:
        a["daily_streak"] = 1
    streak = a["daily_streak"]
    streak_bonus_pct = min(streak - 1, 9) * 5  # +5%/day, caps at +45% on a 10-day streak
    amt = int(amt * (1 + streak_bonus_pct / 100))
    streak_note = f" (day {streak} streak, +{streak_bonus_pct}%)" if streak_bonus_pct else ""
    extra = ""
    if a.get("charm"):
        amt *= 2
        a["charm"] = False
        extra = " (charm doubled it)"
    a["bal"] += amt
    a["last_daily"] = now
    interest_note = ""
    if a["bank"] > 0:
        rate = get_setting("bank_interest", BANK_INTEREST)
        interest = int(a["bank"] * rate)
        if interest > 0:
            a["bank"] += interest
            interest_note = f"\nbank also earned {chips(interest)} interest"
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"+{chips(amt)}{extra}{streak_note}, now at {chips(a['bal'])}{interest_note}")


@bot.slash_command(name="daily", description="claim your daily chips")
async def daily(ctx):
    await do_daily(ctx)


@bot.command(name="daily")
async def daily_cmd(ctx):
    await do_daily(ctx)


async def do_work(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    cooldown = WORK_WAIT - (900 if a["upgrades"].get("resume") else 0)  # resume upgrade: -15m
    left = cooldown - (time.time() - a["last_work"])
    if left > 0:
        m = int(left / 60)
        await reply(ctx, content=f"tired from last shift, {m}m till you can work again", ephemeral=True)
        return
    earned = random.randint(50, 150)
    briefcase_note = ""
    if a["upgrades"].get("briefcase"):
        earned = int(earned * 1.25)
        briefcase_note = " (briefcase bonus)"
    a["bal"] += earned
    a["last_work"] = time.time()
    save(econ, econ_file_for(space))
    jobs = ["waited tables", "walked some dogs", "did food delivery", "mowed a lawn", "fixed someone's pc"]
    await reply(ctx, content=f"you {random.choice(jobs)} and earned {chips(earned)}{briefcase_note}. bal: {chips(a['bal'])}")


@bot.slash_command(name="work", description="work a small job for some chips")
async def work(ctx):
    await do_work(ctx)


@bot.command(name="work")
async def work_cmd(ctx):
    await do_work(ctx)


async def do_crime(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    left = CRIME_WAIT - (time.time() - a["last_crime"])
    if left > 0:
        m = int(left / 60)
        await reply(ctx, content=f"lay low for {m}m before pulling something else", ephemeral=True)
        return
    a["last_crime"] = time.time()
    if random.random() < 0.6:
        earned = random.randint(200, 500)
        a["bal"] += earned
        crimes = ["pickpocketed a tourist", "ran a fake raffle", "hustled some pool", "flipped stolen goods"]
        await reply(ctx, content=f"you {random.choice(crimes)} and got away with {chips(earned)}. bal: {chips(a['bal'])}")
    else:
        fine = max(0, min(random.randint(100, 300), a["bal"]))
        a["bal"] -= fine
        await reply(ctx, content=f"got caught red-handed and paid a {chips(fine)} fine. bal: {chips(a['bal'])}")
    save(econ, econ_file_for(space))


@bot.slash_command(name="crime", description="riskier than /work - bigger payout, chance of a fine")
async def crime(ctx):
    await do_crime(ctx)


@bot.command(name="crime")
async def crime_cmd(ctx):
    await do_crime(ctx)


async def do_beg(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    left = BEG_WAIT - (time.time() - a["last_beg"])
    if left > 0:
        m = int(left / 60) + 1
        await reply(ctx, content=f"give it a minute, {m}m left before you can beg again", ephemeral=True)
        return
    a["last_beg"] = time.time()
    if random.random() < 0.1:
        save(econ, econ_file_for(space))
        await reply(ctx, content="nobody had any change on them. bal: " + chips(a["bal"]))
        return
    earned = random.randint(5, 40)
    a["bal"] += earned
    save(econ, econ_file_for(space))
    lines = ["a stranger tossed you some spare change", "someone felt bad and chipped in", "you found a few chips on the ground"]
    await reply(ctx, content=f"{random.choice(lines)}, +{chips(earned)}. bal: {chips(a['bal'])}")


@bot.slash_command(name="beg", description="quick, tiny, no risk - good for topping up between cooldowns")
async def beg(ctx):
    await do_beg(ctx)


@bot.command(name="beg")
async def beg_cmd(ctx):
    await do_beg(ctx)


async def do_give(ctx, user, amount):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if user.id == ctx.author.id:
        await reply(ctx, content="cant pay yourself lol", ephemeral=True)
        return
    if user.bot:
        await reply(ctx, content="bots dont need chips", ephemeral=True)
        return
    econ = loadEcon()
    a, b = acct(econ, ctx.guild, ctx.author), acct(econ, ctx.guild, user)
    if a["bal"] < amount:
        await reply(ctx, content=f"you only have {chips(a['bal'])}", ephemeral=True)
        return
    a["bal"] -= amount
    b["bal"] += amount
    save(econ)
    await reply(ctx, content=f"{ctx.author.mention} sent {chips(amount)} to {user.mention}")


@bot.slash_command(name="give", description="send chips to someone")
async def give(ctx, user: Option(discord.Member, "who to pay"), amount: Option(int, "how much", min_value=1)):
    await do_give(ctx, user, amount)


@bot.command(name="give", aliases=["pay"])
async def give_cmd(ctx, user: discord.Member, amount: int):
    if amount < 1:
        await reply(ctx, content="amount has to be positive", ephemeral=True)
        return
    await do_give(ctx, user, amount)


async def do_rob(ctx, target):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if target.id == ctx.author.id:
        await reply(ctx, content="cant rob yourself", ephemeral=True)
        return
    if target.bot:
        await reply(ctx, content="nothing to steal from a bot", ephemeral=True)
        return
    econ = loadEcon()
    a, t = acct(econ, ctx.guild, ctx.author), acct(econ, ctx.guild, target)
    if t.get("shield_until", 0) > time.time():
        await reply(ctx, content=f"{target.display_name} bought a shield, can't touch them right now", ephemeral=True)
        return
    left = ROB_WAIT - (time.time() - a["last_rob"])
    if left > 0:
        m = int(left / 60)
        await reply(ctx, content=f"lay low for {m}m before trying that again", ephemeral=True)
        return
    if t["bal"] < 100:
        await reply(ctx, content=f"{target.display_name} doesnt have enough to bother robbing", ephemeral=True)
        return
    a["last_rob"] = time.time()
    used_lockpick = bool(a.get("lockpick"))
    if used_lockpick:
        a["lockpick"] = False
    if used_lockpick or random.random() < 0.4:
        if t.get("insurance"):
            t["insurance"] = False
            save(econ)
            await reply(ctx, content=f"{target.display_name} had insurance active - the robbery was blocked, no chips stolen")
            return
        stolen = random.randint(int(t["bal"] * 0.1), int(t["bal"] * 0.3))
        t["bal"] -= stolen
        a["bal"] += stolen
        bounty_note = ""
        all_cfg = loadCfg()
        g = get_guild_cfg(all_cfg, ctx.guild)
        bounties = g.get("bounties", {})
        tid = str(target.id)
        if bounties.get(tid):
            payout = bounties.pop(tid)
            a["bal"] += payout
            saveCfg(all_cfg)
            log_event(ctx.guild.name, f"{ctx.author.display_name} claimed a {chips(payout)} bounty on {target.display_name}")
            bounty_note = f"\n💰 bounty claimed: +{chips(payout)}"
        lockpick_note = " (lockpick guaranteed it)" if used_lockpick else ""
        save(econ)
        await reply(ctx, content=f"you robbed {target.mention} for {chips(stolen)}!{lockpick_note} bal: {chips(a['bal'])}{bounty_note}")
    else:
        fine = min(random.randint(75, 200), a["bal"])
        a["bal"] -= fine
        save(econ)
        await reply(ctx, content=f"got caught trying to rob {target.mention} and paid a {chips(fine)} fine")


@bot.slash_command(name="rob", description="try to steal chips off someone, risky")
async def rob(ctx, user: Option(discord.Member, "who to rob")):
    await do_rob(ctx, user)


@bot.command(name="rob")
async def rob_cmd(ctx, user: discord.Member):
    await do_rob(ctx, user)


async def do_leaderboard(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    econ = loadEcon()
    g = ensure_guild(econ, ctx.guild)
    save(econ)
    ranked = sorted(g["users"].items(), key=lambda kv: kv[1]["bal"], reverse=True)[:10]
    if not ranked:
        await reply(ctx, content="nobody's played yet")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for n, (uid, a) in enumerate(ranked):
        m = ctx.guild.get_member(int(uid))
        nm = m.display_name if m else a.get("name", "user " + uid)
        tag = medals[n] if n < 3 else f"{n + 1}."
        lines.append(f"{tag} {nm} - {chips(a['bal'])}")
    await reply(ctx, embed=discord.Embed(title="leaderboard", description="\n".join(lines)))


@bot.slash_command(name="leaderboard", description="whos rich in this server")
async def leaderboard(ctx):
    await do_leaderboard(ctx)


@bot.command(name="leaderboard", aliases=["lb"])
async def leaderboard_cmd(ctx):
    await do_leaderboard(ctx)


# ================= BANK =================
# money in the bank is safe from /rob and earns a little interest each time
# you claim /daily, but it's not spendable on bets til you move it back
async def do_deposit(ctx, amount):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="cant deposit more than you have on hand", ephemeral=True)
        return
    a["bal"] -= amount
    a["bank"] += amount
    save(econ, econ_file_for(space))
    if is_real_guild(space):
        await logging_utils.send_log(bot, space, "deposit",
                                      embeds.transaction_embed("deposit", ctx.author, amount, a["bal"], a["bank"]))
    await reply(ctx, content=f"tucked away {chips(amount)}. wallet {chips(a['bal'])}, bank {chips(a['bank'])}")


@bot.slash_command(name="deposit", description="move chips into your bank, safe from robbery")
async def deposit(ctx, amount: Option(int, "how much", min_value=1)):
    await do_deposit(ctx, amount)


@bot.command(name="deposit", aliases=["dep"])
async def deposit_cmd(ctx, amount: int):
    await do_deposit(ctx, amount)


async def do_withdraw(ctx, amount):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bank"]:
        await reply(ctx, content="you dont have that much in the bank", ephemeral=True)
        return
    a["bank"] -= amount
    a["bal"] += amount
    save(econ, econ_file_for(space))
    if is_real_guild(space):
        await logging_utils.send_log(bot, space, "withdraw",
                                      embeds.transaction_embed("withdraw", ctx.author, amount, a["bal"], a["bank"]))
    await reply(ctx, content=f"pulled out {chips(amount)}. wallet {chips(a['bal'])}, bank {chips(a['bank'])}")


@bot.slash_command(name="withdraw", description="move chips from your bank back to your wallet")
async def withdraw(ctx, amount: Option(int, "how much", min_value=1)):
    await do_withdraw(ctx, amount)


@bot.command(name="withdraw", aliases=["wd"])
async def withdraw_cmd(ctx, amount: int):
    await do_withdraw(ctx, amount)


# ================= DATA DELETION (Discord Developer Policy requirement) =================
# lets someone wipe their own stored data - required per Discord's Developer
# Policy for any app that collects user data, and specifically called out in
# the Privileged Intent review process. covers everything this bot actually
# stores about a person outside of Discord itself: their casino/economy
# account (balance, bank, inventory, business, achievements) and, in a real
# server, their moderation warning history. does NOT touch anything that
# only exists as ordinary messages in Discord's own systems (e.g. things
# this bot has said about them in a log channel) - deleting those isn't
# meaningfully different from any other message in the server and Discord
# is the system of record for those, not this bot.
async def do_deletemydata(ctx):
    space = get_space(ctx)
    gid = str(space.id)
    uid = str(ctx.author.id)
    removed = []

    econ = loadEcon(econ_file_for(space))
    if gid in econ and uid in econ[gid].get("users", {}):
        del econ[gid]["users"][uid]
        save(econ, econ_file_for(space))
        removed.append("your casino/economy account (balance, bank, inventory, business, achievements)")

    if is_real_guild(space):
        warns = load_warnings()
        if warns.get(gid, {}).get(uid):
            warns[gid][uid] = []
            save_warnings(warns)
            removed.append("your moderation warning history")

    if removed:
        where = "this server" if is_real_guild(space) else "your personal ledger"
        await reply(ctx, content=f"🗑️ deleted {' and '.join(removed)} for {where}. this can't be undone.",
                    ephemeral=True)
    else:
        await reply(ctx, content="couldn't find any stored data for you here to delete.", ephemeral=True)


@bot.slash_command(name="deletemydata", description="permanently delete your stored casino/economy data and warning history")
async def deletemydata(ctx):
    await do_deletemydata(ctx)


@bot.command(name="deletemydata")
async def deletemydata_cmd(ctx):
    await do_deletemydata(ctx)


# ================= LOANS =================
async def do_loan(ctx, amount):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a.get("loan"):
        owed = a["loan"]["owed"]
        await reply(ctx, content=f"pay off your existing loan first, you still owe {chips(owed)}", ephemeral=True)
        return
    loan_max = get_setting("loan_max", LOAN_MAX)
    if amount <= 0 or amount > loan_max:
        await reply(ctx, content=f"loans are capped at {chips(loan_max)}", ephemeral=True)
        return
    rate = get_setting("loan_interest", LOAN_INTEREST)
    halved = a.get("vault_interest_until", 0) > time.time()
    if halved:
        rate = rate / 2
    owed = int(amount * (1 + rate))
    a["loan"] = {"owed": owed, "due": time.time() + LOAN_TERM, "defaulted": False}
    a["bal"] += amount
    award(a, "first_loan", "First Loan", space.name)
    save(econ, econ_file_for(space))
    log_event(space.name, f"{ctx.author.display_name} took a loan of {chips(amount)}")
    note = " (vault halved your rate)" if halved else ""
    await reply(ctx, content=f"borrowed {chips(amount)}, you owe {chips(owed)} ({int(rate*100)}% interest{note}) within 24h or the house takes it automatically. bal {chips(a['bal'])}")


@bot.slash_command(name="loan", description="borrow chips from the house, 20% interest, 24h to repay")
async def loan(ctx, amount: Option(int, "how much", min_value=1)):
    await do_loan(ctx, amount)


@bot.command(name="loan")
async def loan_cmd(ctx, amount: int):
    await do_loan(ctx, amount)


async def do_repay(ctx, amount):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if not a.get("loan"):
        await reply(ctx, content="you don't owe anything", ephemeral=True)
        return
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="cant repay more than you have", ephemeral=True)
        return
    owed = a["loan"]["owed"]
    pay = min(amount, owed)
    a["bal"] -= pay
    a["loan"]["owed"] -= pay
    cleared = a["loan"]["owed"] <= 0
    if cleared:
        a["loan"] = None
        award(a, "paid_in_full", "Paid In Full", space.name)
        log_event(space.name, f"{ctx.author.display_name} paid off their loan in full")
    save(econ, econ_file_for(space))
    if cleared:
        await reply(ctx, content=f"paid off {chips(pay)}, loan cleared. bal {chips(a['bal'])}")
    else:
        await reply(ctx, content=f"paid {chips(pay)}, still owe {chips(a['loan']['owed'])}. bal {chips(a['bal'])}")


@bot.slash_command(name="repay", description="pay back part or all of your loan")
async def repay(ctx, amount: Option(int, "how much", min_value=1)):
    await do_repay(ctx, amount)


@bot.command(name="repay")
async def repay_cmd(ctx, amount: int):
    await do_repay(ctx, amount)


async def do_debtors(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    econ = loadEcon()
    g = ensure_guild(econ, ctx.guild)
    save(econ)
    debtors = []
    for u in g["users"].values():
        loan = u.get("loan")
        if loan and loan.get("owed", 0) > 0:
            debtors.append((u["name"], loan["owed"], loan.get("defaulted", False)))
    if not debtors:
        await reply(ctx, content="nobody owes the house anything right now")
        return
    debtors.sort(key=lambda d: -d[1])
    lines = [f"{'💀 ' if d[2] else ''}{d[0]}: owes {chips(d[1])}" for d in debtors[:15]]
    await reply(ctx, embed=discord.Embed(title="debtors", description="\n".join(lines), color=discord.Color.gold()))


@bot.slash_command(name="debtors", description="see who owes the house money")
async def debtors(ctx):
    await do_debtors(ctx)


@bot.command(name="debtors")
async def debtors_cmd(ctx):
    await do_debtors(ctx)


async def do_forgive(ctx, user):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    econ = loadEcon()
    a = acct(econ, ctx.guild, user)
    if not a.get("loan"):
        await reply(ctx, content=f"{user.display_name} doesn't owe anything", ephemeral=True)
        return
    owed = a["loan"]["owed"]
    a["loan"] = None
    save(econ)
    log_event(ctx.guild.name, f"{ctx.author.display_name} forgave {user.display_name}'s debt of {chips(owed)}")
    await reply(ctx, content=f"wiped {user.mention}'s {chips(owed)} debt clean")


@bot.slash_command(name="forgive", description="manager - wipe someone's debt clean")
async def forgive(ctx, user: Option(discord.Member, "who")):
    await do_forgive(ctx, user)


@bot.command(name="forgive")
async def forgive_cmd(ctx, user: discord.Member):
    await do_forgive(ctx, user)


# ================= SHOP =================
BUYABLE = {k: v for k, v in SHOP.items() if v["price"] > 0}


async def do_shop(ctx):
    lines = [f"**{name}** - {chips(item['price'])} - {item['desc']}" for name, item in BUYABLE.items()]
    lines.append(f"**vault** - craft only (3 shields) - {SHOP['vault']['desc']}")
    await reply(ctx, embed=discord.Embed(
        title="shop",
        description="\n".join(lines) + "\n\nbuy with /buy <item>, then /use <item> to activate it. check /inventory anytime."
    ))


@bot.slash_command(name="shop", description="see what chips can buy")
async def shop(ctx):
    await do_shop(ctx)


@bot.command(name="shop")
async def shop_cmd(ctx):
    await do_shop(ctx)


async def do_buy(ctx, item):
    space = get_space(ctx)
    item = item.lower().strip()
    if item not in BUYABLE:
        await reply(ctx, content="not selling that, check /shop", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    price = BUYABLE[item]["price"]
    if a["bal"] < price:
        await reply(ctx, content=f"need {chips(price)}, you have {chips(a['bal'])}", ephemeral=True)
        return
    a["bal"] -= price
    a["inv"][item] = a["inv"].get(item, 0) + 1
    save(econ, econ_file_for(space))
    log_event(space.name, f"{ctx.author.display_name} bought {item} for {chips(price)}")
    await reply(ctx, content=f"bought a **{item}** for {chips(price)} - use it with /use {item}. bal {chips(a['bal'])}")


@bot.slash_command(name="buy", description="buy something from the shop")
async def buy(ctx, item: Option(str, "which item", choices=list(BUYABLE.keys()))):
    await do_buy(ctx, item)


@bot.command(name="buy")
async def buy_cmd(ctx, item: str):
    await do_buy(ctx, item)


async def do_use(ctx, item):
    space = get_space(ctx)
    item = item.lower().strip()
    if item not in SHOP:
        await reply(ctx, content="don't have that", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a["inv"].get(item, 0) <= 0:
        await reply(ctx, content=f"you don't have a spare {item}, buy one first", ephemeral=True)
        return
    a["inv"][item] -= 1
    if item == "shield":
        a["shield_until"] = time.time() + 86400
        msg = "shielded for 24h - nobody can /rob you"
    elif item == "charm":
        a["charm"] = True
        msg = "your next /daily claim pays double"
    elif item == "insurance":
        a["insurance"] = True
        msg = "insured - the next successful rob against you will be blocked instead"
    elif item == "vault":
        a["shield_until"] = time.time() + 172800
        a["vault_interest_until"] = time.time() + 172800
        msg = "shielded 48h AND any loan taken in that window is half interest"
    elif item == "lockpick":
        a["lockpick"] = True
        msg = "your next /rob is guaranteed to succeed (a shield on the target still blocks it)"
    elif item == "energy_drink":
        a["last_work"] = 0
        a["last_crime"] = 0
        msg = "cooldowns reset - /work and /crime are ready again"
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"used a **{item}** - {msg}. {a['inv'][item]} left in inventory")


@bot.slash_command(name="use", description="activate something from your inventory")
async def use(ctx, item: Option(str, "which item", choices=list(SHOP.keys()))):
    await do_use(ctx, item)


@bot.command(name="use")
async def use_cmd(ctx, item: str):
    await do_use(ctx, item)


async def do_craft(ctx, item):
    space = get_space(ctx)
    item = item.lower().strip()
    if item != "vault":
        await reply(ctx, content="only vault can be crafted right now (3 shields)", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a["inv"].get("shield", 0) < 3:
        await reply(ctx, content=f"need 3 shields, you have {a['inv'].get('shield', 0)}", ephemeral=True)
        return
    a["inv"]["shield"] -= 3
    a["inv"]["vault"] = a["inv"].get("vault", 0) + 1
    save(econ, econ_file_for(space))
    log_event(space.name, f"{ctx.author.display_name} crafted a vault")
    await reply(ctx, content=f"crafted a **vault**! use it with /use vault. inventory: {a['inv']}")


@bot.slash_command(name="craft", description="combine items into something better")
async def craft(ctx, item: Option(str, "what to craft", choices=["vault"])):
    await do_craft(ctx, item)


@bot.command(name="craft")
async def craft_cmd(ctx, item: str):
    await do_craft(ctx, item)


async def do_inventory(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    save(econ, econ_file_for(space))
    lines = [f"{k}: {v}" for k, v in a["inv"].items() if v > 0]
    desc = "\n".join(lines) if lines else "empty - check /shop"
    if a.get("investments"):
        desc += "\n\nlocked investments:\n" + "\n".join(
            f"{chips(i['amount'])} -> matures for {chips(i['payout'])} <t:{int(i['matures'])}:R>" for i in a["investments"]
        )
    biz = a.get("business")
    if biz:
        tier = BUSINESS_TIERS[biz["tier"]]
        desc += f"\n\nbusiness: {tier['label']} ({chips(tier['hourly'])}/hr, check /business to collect)"
    owned_upgrades = [name for name in UPGRADES if a.get("upgrades", {}).get(name)]
    if owned_upgrades:
        desc += "\n\nupgrades: " + ", ".join(owned_upgrades)
    await reply(ctx, embed=discord.Embed(title=f"{ctx.author.display_name}'s inventory", description=desc))


@bot.slash_command(name="inventory", description="see your shop items and locked investments")
async def inventory(ctx):
    await do_inventory(ctx)


@bot.command(name="inventory", aliases=["inv"])
async def inventory_cmd(ctx):
    await do_inventory(ctx)


BADGE_LABELS = {
    "first_loan": "First Loan",
    "paid_in_full": "Paid In Full",
    "jackpot_winner": "Jackpot Winner",
    "survived_default": "Survived a Default",
    "high_roller": "High Roller (10k+)",
    "whale": "Whale (5k+ single bet)",
    "comeback_kid": "Comeback Kid",
    "win_streak": "On Fire (5-win streak)",
}


async def do_achievements(ctx, who):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, who)
    save(econ, econ_file_for(space))
    badges = a.get("badges", [])
    if not badges:
        desc = "no badges yet"
    else:
        desc = "\n".join(f"🏅 {BADGE_LABELS.get(b, b)}" for b in badges)
    await reply(ctx, embed=discord.Embed(title=f"{who.display_name}'s achievements", description=desc))


@bot.slash_command(name="achievements", description="see unlocked badges")
async def achievements(ctx, user: Option(discord.Member, "who", required=False) = None):
    await do_achievements(ctx, user or ctx.author)


@bot.command(name="achievements", aliases=["badges"])
async def achievements_cmd(ctx, user: discord.Member = None):
    await do_achievements(ctx, user or ctx.author)


# ================= PAWN SHOP =================
# sell shop items back for half what you paid - lets people convert an
# item they don't need into chips instead of it just sitting in inventory
PAWN_REFUND_PCT = 0.5


async def do_pawn(ctx, item):
    space = get_space(ctx)
    item = item.lower().strip()
    if item not in SHOP or SHOP[item]["price"] <= 0:
        await reply(ctx, content="that one's not pawnable (either not a real item or craft-only)", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a["inv"].get(item, 0) <= 0:
        await reply(ctx, content=f"you don't have a {item} to pawn", ephemeral=True)
        return
    refund = int(SHOP[item]["price"] * PAWN_REFUND_PCT)
    a["inv"][item] -= 1
    a["bal"] += refund
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"pawned a **{item}** for {chips(refund)} ({int(PAWN_REFUND_PCT*100)}% refund). bal {chips(a['bal'])}")


@bot.slash_command(name="pawn", description="sell a shop item back for half its price")
async def pawn(ctx, item: Option(str, "which item", choices=list(BUYABLE.keys()))):
    await do_pawn(ctx, item)


@bot.command(name="pawn")
async def pawn_cmd(ctx, item: str):
    await do_pawn(ctx, item)


# ================= BUSINESSES (passive income) =================
async def do_business(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    save(econ, econ_file_for(space))
    biz = a.get("business")
    lines = []
    if biz:
        tier = BUSINESS_TIERS[biz["tier"]]
        elapsed_h = min((time.time() - biz["last_collect"]) / 3600, BUSINESS_CAP_HOURS)
        pending = int(tier["hourly"] * elapsed_h)
        lines.append(f"you own: **{tier['label']}** - {chips(tier['hourly'])}/hr, uncollected: {chips(pending)}")
        lines.append("collect it with /collect (income caps at 24h uncollected, so don't let it sit forever)")
    else:
        lines.append("you don't own a business yet")
    lines.append("")
    lines.append("tiers - `/buybusiness <tier>` (upgrading auto-collects and fully replaces your current one):")
    for tier_key in BUSINESS_ORDER:
        t = BUSINESS_TIERS[tier_key]
        lines.append(f"**{tier_key}** ({t['label']}) - {chips(t['price'])}, {chips(t['hourly'])}/hr")
    await reply(ctx, embed=discord.Embed(title="businesses", description="\n".join(lines), color=discord.Color.gold()))


@bot.slash_command(name="business", description="see your business and what's available to buy")
async def business(ctx):
    await do_business(ctx)


@bot.command(name="business", aliases=["biz"])
async def business_cmd(ctx):
    await do_business(ctx)


async def do_buybusiness(ctx, tier):
    space = get_space(ctx)
    tier = tier.lower().strip()
    if tier not in BUSINESS_TIERS:
        await reply(ctx, content=f"pick one of: {', '.join(BUSINESS_ORDER)}", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    current = a.get("business")
    if current:
        if BUSINESS_ORDER.index(current["tier"]) >= BUSINESS_ORDER.index(tier):
            await reply(ctx, content=f"you already own {BUSINESS_TIERS[current['tier']]['label']} or better", ephemeral=True)
            return
        # auto-collect whatever built up before switching so nothing's lost
        old_tier = BUSINESS_TIERS[current["tier"]]
        elapsed_h = min((time.time() - current["last_collect"]) / 3600, BUSINESS_CAP_HOURS)
        pending = int(old_tier["hourly"] * elapsed_h)
        a["bal"] += pending
    price = BUSINESS_TIERS[tier]["price"]
    if a["bal"] < price:
        await reply(ctx, content=f"need {chips(price)}, you have {chips(a['bal'])}", ephemeral=True)
        return
    a["bal"] -= price
    a["business"] = {"tier": tier, "last_collect": time.time()}
    save(econ, econ_file_for(space))
    log_event(space.name, f"{ctx.author.display_name} bought the {BUSINESS_TIERS[tier]['label']} business")
    await reply(ctx, content=f"bought the **{BUSINESS_TIERS[tier]['label']}** - earning {chips(BUSINESS_TIERS[tier]['hourly'])}/hr, claim it with /collect. bal {chips(a['bal'])}")


@bot.slash_command(name="buybusiness", description="buy or upgrade your passive-income business")
async def buybusiness(ctx, tier: Option(str, "which tier", choices=BUSINESS_ORDER)):
    await do_buybusiness(ctx, tier)


@bot.command(name="buybusiness")
async def buybusiness_cmd(ctx, tier: str):
    await do_buybusiness(ctx, tier)


async def do_collect(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    biz = a.get("business")
    if not biz:
        await reply(ctx, content="you don't own a business yet, check /business", ephemeral=True)
        return
    tier = BUSINESS_TIERS[biz["tier"]]
    elapsed_h = min((time.time() - biz["last_collect"]) / 3600, BUSINESS_CAP_HOURS)
    earned = int(tier["hourly"] * elapsed_h)
    if earned <= 0:
        await reply(ctx, content="nothing built up yet, check back later", ephemeral=True)
        return
    a["bal"] += earned
    biz["last_collect"] = time.time()
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"collected {chips(earned)} from your {tier['label']}. bal {chips(a['bal'])}")


@bot.slash_command(name="collect", description="claim income from your business")
async def collect(ctx):
    await do_collect(ctx)


@bot.command(name="collect")
async def collect_cmd(ctx):
    await do_collect(ctx)


# ================= PERMANENT UPGRADES =================
async def do_upgrades(ctx):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    save(econ, econ_file_for(space))
    lines = []
    for name, u in UPGRADES.items():
        owned = " (owned)" if a.get("upgrades", {}).get(name) else f" - {chips(u['price'])}"
        lines.append(f"**{name}**{owned} - {u['desc']}")
    lines.append("\nbuy with `/buyupgrade <name>` - one-time, permanent, never used up or pawned")
    await reply(ctx, embed=discord.Embed(title="upgrades", description="\n".join(lines), color=discord.Color.gold()))


@bot.slash_command(name="upgrades", description="see permanent upgrades you can buy")
async def upgrades(ctx):
    await do_upgrades(ctx)


@bot.command(name="upgrades")
async def upgrades_cmd(ctx):
    await do_upgrades(ctx)


async def do_buyupgrade(ctx, name):
    space = get_space(ctx)
    name = name.lower().strip()
    if name not in UPGRADES:
        await reply(ctx, content=f"pick one of: {', '.join(UPGRADES.keys())}", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a.get("upgrades", {}).get(name):
        await reply(ctx, content=f"you already own **{name}**", ephemeral=True)
        return
    price = UPGRADES[name]["price"]
    if a["bal"] < price:
        await reply(ctx, content=f"need {chips(price)}, you have {chips(a['bal'])}", ephemeral=True)
        return
    a["bal"] -= price
    a["upgrades"][name] = True
    save(econ, econ_file_for(space))
    log_event(space.name, f"{ctx.author.display_name} bought the {name} upgrade")
    await reply(ctx, content=f"bought **{name}** - {UPGRADES[name]['desc']}. bal {chips(a['bal'])}")


@bot.slash_command(name="buyupgrade", description="buy a permanent upgrade")
async def buyupgrade(ctx, name: Option(str, "which upgrade", choices=list(UPGRADES.keys()))):
    await do_buyupgrade(ctx, name)


@bot.command(name="buyupgrade")
async def buyupgrade_cmd(ctx, name: str):
    await do_buyupgrade(ctx, name)


# ================= BOUNTIES =================
# anyone can put chips on someone's head - first person to land a
# successful /rob against that target claims the whole pot on top of
# their normal steal
async def do_bounty(ctx, target, amount):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if target.id == ctx.author.id:
        await reply(ctx, content="cant put a bounty on yourself", ephemeral=True)
        return
    if target.bot:
        await reply(ctx, content="bots dont have bounties", ephemeral=True)
        return
    econ = loadEcon()
    a = acct(econ, ctx.guild, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad amount", ephemeral=True)
        return
    a["bal"] -= amount
    save(econ)
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    bounties = g.setdefault("bounties", {})
    tid = str(target.id)
    bounties[tid] = bounties.get(tid, 0) + amount
    saveCfg(all_cfg)
    log_event(ctx.guild.name, f"{ctx.author.display_name} put a {chips(amount)} bounty on {target.display_name}")
    await reply(ctx, content=f"💰 bounty placed: {chips(bounties[tid])} total on {target.mention} - the next successful /rob against them claims it")


@bot.slash_command(name="bounty", description="put chips on someone's head, claimed via a successful rob")
async def bounty(ctx, user: Option(discord.Member, "who"), amount: Option(int, "how much", min_value=1)):
    await do_bounty(ctx, user, amount)


@bot.command(name="bounty")
async def bounty_cmd(ctx, user: discord.Member, amount: int):
    await do_bounty(ctx, user, amount)


async def do_bounties(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    g = get_guild_cfg(loadCfg(), ctx.guild)
    bounties = g.get("bounties", {})
    if not bounties:
        await reply(ctx, content="no active bounties right now")
        return
    lines = []
    for uid, amt in sorted(bounties.items(), key=lambda kv: -kv[1]):
        m = ctx.guild.get_member(int(uid))
        nm = m.display_name if m else f"user {uid}"
        lines.append(f"{nm}: {chips(amt)}")
    await reply(ctx, embed=discord.Embed(title="active bounties", description="\n".join(lines), color=discord.Color.red()))


@bot.slash_command(name="bounties", description="see who has a bounty on them")
async def bounties(ctx):
    await do_bounties(ctx)


@bot.command(name="bounties")
async def bounties_cmd(ctx):
    await do_bounties(ctx)


# ================= INVESTMENTS =================
INVEST_RATES = {1: 0.03, 3: 0.10, 7: 0.25}  # days -> total return, better than the bank's daily 2%


async def do_invest(ctx, amount, days):
    space = get_space(ctx)
    if days not in INVEST_RATES:
        await reply(ctx, content=f"pick a term: {', '.join(str(d) for d in INVEST_RATES)} days", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad amount", ephemeral=True)
        return
    payout = int(amount * (1 + INVEST_RATES[days]))
    a["bal"] -= amount
    a["investments"].append({"amount": amount, "payout": payout, "matures": time.time() + days * 86400})
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"locked up {chips(amount)} for {days}d, matures to {chips(payout)}. check /inventory for status")


@bot.slash_command(name="invest", description="lock chips up for better returns than the bank")
async def invest(ctx, amount: Option(int, "how much", min_value=1), days: Option(int, "term length", choices=list(INVEST_RATES.keys()))):
    await do_invest(ctx, amount, days)


@bot.command(name="invest")
async def invest_cmd(ctx, amount: int, days: int):
    await do_invest(ctx, amount, days)


# ================= GAMES =================
async def do_coinflip(ctx, amount, side):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    rig = pop_rig(space, ctx.author)
    if rig == "win":
        result = side
    elif rig == "lose":
        result = "tails" if side == "heads" else "heads"
    else:
        result = random.choice(["heads", "tails"])
    bal_before = a["bal"]
    if result == side:
        a["bal"] += amount
        msg = f"landed {result}, you won {chips(amount)}! bal: {chips(a['bal'])}"
        net = amount
    else:
        a["bal"] -= amount
        msg = f"landed {result}, lost {chips(amount)}. bal: {chips(a['bal'])}"
        net = -amount
    check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=msg)


@bot.slash_command(name="coinflip", description="flip a coin, double or nothing")
async def coinflip(ctx, amount: Option(int, "bet", min_value=1), side: Option(str, "pick one", choices=["heads", "tails"])):
    await do_coinflip(ctx, amount, side)


@bot.command(name="coinflip", aliases=["cf"])
async def coinflip_cmd(ctx, amount: int, side: str):
    side = side.lower()
    if side not in ("heads", "tails"):
        await reply(ctx, content="pick heads or tails", ephemeral=True)
        return
    await do_coinflip(ctx, amount, side)


# cherry is common, sevens basically never hit - had to nerf sevens after
# someone got lucky twice in a row and drained half the leaderboard's chips
SYMS = {"🍒": 40, "🍋": 25, "🔔": 15, "⭐": 10, "💎": 5, "7️⃣": 2}


async def do_slots(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0:
        await reply(ctx, content="bet something real", ephemeral=True)
        return
    if amount > a["bal"]:
        await reply(ctx, content="not enough chips", ephemeral=True)
        return

    rig = pop_rig(space, ctx.author)
    if rig == "win":
        reels = ["🍒", "🍒", "🍒"]  # modest guaranteed win, not a jackpot
    elif rig == "lose":
        syms = list(SYMS.keys())
        while True:
            reels = random.choices(syms, weights=list(SYMS.values()), k=3)
            if len(set(reels)) == 3:  # no pair, no jackpot
                break
    else:
        reels = random.choices(list(SYMS.keys()), weights=list(SYMS.values()), k=3)

    # yeah this is just a chain of ifs, i know. works fine, not touching it
    won = 0
    if reels[0] == reels[1] == reels[2]:
        s = reels[0]
        if s == "7️⃣":
            won = amount * 20
        elif s == "💎":
            won = amount * 10
        elif s == "⭐":
            won = amount * 6
        elif s == "🔔":
            won = amount * 4
        else:
            won = amount * 3
    elif reels[0] == reels[1] or reels[1] == reels[2] or reels[0] == reels[2]:
        won = int(amount * 1.2)

    # every non-jackpot spin chips a little into the pool, triple 7s takes
    # the whole thing on top of the normal payout then resets it
    all_cfg = loadCfg()
    gcfg = get_guild_cfg(all_cfg, space)
    jackpot_note = ""
    if reels[0] == reels[1] == reels[2] == "7️⃣":
        pool = gcfg["jackpot"]
        won += pool
        gcfg["jackpot"] = 500
        jackpot_note = f"\n🎉 JACKPOT on top of that: +{chips(pool)}"
        log_event(space.name, f"{ctx.author.display_name} hit the jackpot for {chips(pool)}!")
    else:
        gcfg["jackpot"] += max(1, int(amount * 0.02))
    saveCfg(all_cfg)

    a["bal"] = a["bal"] - amount + won
    net = won - amount
    check_bet_badges(a, space.name, amount, a["bal"] - net, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"[ {' '.join(reels)} ]\n{'won' if net>=0 else 'lost'} {chips(abs(net))} - bal {chips(a['bal'])}{jackpot_note}")


@bot.slash_command(name="slots", description="spin it")
async def slots(ctx, amount: Option(int, "bet", min_value=1)):
    await do_slots(ctx, amount)


@bot.command(name="slots")
async def slots_cmd(ctx, amount: int):
    await do_slots(ctx, amount)


async def do_jackpot(ctx):
    space = get_space(ctx)
    g = get_guild_cfg(loadCfg(), space)
    await reply(ctx, content=f"current jackpot: {chips(g['jackpot'])} - land triple 7️⃣ on /slots to take it")


@bot.slash_command(name="jackpot", description="see the current slots jackpot")
async def jackpot(ctx):
    await do_jackpot(ctx)


@bot.command(name="jackpot")
async def jackpot_cmd(ctx):
    await do_jackpot(ctx)


async def do_dice(ctx, amount, guess):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    if guess < 1 or guess > 6:
        await reply(ctx, content="guess has to be 1-6", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    rig = pop_rig(space, ctx.author)
    if rig == "win":
        roll = guess
    elif rig == "lose":
        roll = random.choice([n for n in range(1, 7) if n != guess])
    else:
        roll = random.randint(1, 6)
    bal_before = a["bal"]
    if roll == guess:
        a["bal"] += amount * 5
        msg = f"rolled a {roll}, nailed it! won {chips(amount * 5)}. bal: {chips(a['bal'])}"
        net = amount * 5
    else:
        a["bal"] -= amount
        msg = f"rolled a {roll}, missed your {guess}. lost {chips(amount)}. bal: {chips(a['bal'])}"
        net = -amount
    check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=msg)


@bot.slash_command(name="dice", description="guess the roll, 5x payout")
async def dice(ctx, amount: Option(int, "bet", min_value=1), guess: Option(int, "1-6", min_value=1, max_value=6)):
    await do_dice(ctx, amount, guess)


@bot.command(name="dice")
async def dice_cmd(ctx, amount: int, guess: int):
    await do_dice(ctx, amount, guess)


RED_NUMS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}


def roulette_color(n):
    if n == 0:
        return "green"
    return "red" if n in RED_NUMS else "black"


async def do_roulette(ctx, amount, choice):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return

    choice = choice.lower().strip()
    rig = pop_rig(space, ctx.author)
    if rig == "win":
        if choice.isdigit():
            spin = int(choice)
        elif choice in ("red", "black"):
            spin = random.choice([n for n in range(1, 37) if roulette_color(n) == choice])
        elif choice == "green":
            spin = 0
        else:
            spin = random.randint(0, 36)
    elif rig == "lose":
        if choice.isdigit():
            spin = random.choice([n for n in range(0, 37) if n != int(choice)])
        elif choice in ("red", "black"):
            spin = random.choice([n for n in range(1, 37) if roulette_color(n) != choice])
        elif choice == "green":
            spin = random.randint(1, 36)
        else:
            spin = random.randint(0, 36)
    else:
        spin = random.randint(0, 36)
    color = roulette_color(spin)
    won = 0

    if choice.isdigit():
        if int(choice) < 0 or int(choice) > 36:
            await reply(ctx, content="numbers are 0-36", ephemeral=True)
            return
        if int(choice) == spin:
            won = amount * 35
    elif choice in ("red", "black"):
        if choice == color:
            won = amount * 2
    elif choice == "green":
        if color == "green":
            won = amount * 14
    else:
        await reply(ctx, content="bet red, black, green, or a number 0-36", ephemeral=True)
        return

    a["bal"] = a["bal"] - amount + won
    save(econ, econ_file_for(space))
    net = won - amount
    await reply(ctx, content=f"ball landed on {spin} ({color})\n{'won' if net>=0 else 'lost'} {chips(abs(net))} - bal {chips(a['bal'])}")


@bot.slash_command(name="roulette", description="bet on red/black/green or a straight number")
async def roulette(ctx, amount: Option(int, "bet", min_value=1), choice: Option(str, "red, black, green, or 0-36")):
    await do_roulette(ctx, amount, choice)


@bot.command(name="roulette", aliases=["rl"])
async def roulette_cmd(ctx, amount: int, choice: str):
    await do_roulette(ctx, amount, choice)


# ================= ALL IN =================
# quick shortcut for people who don't wanna type out their whole balance -
# just red/black on the wheel, no numbers/green since that'd basically never
# hit and would just be a rage-quit generator. confirm button so a misclick
# doesn't nuke someone's whole stack
class AllInConfirm(discord.ui.View):
    def __init__(self, author, guild, choice, bet, econ):
        super().__init__(timeout=30)
        self.author = author
        self.guild = guild
        self.choice = choice
        self.bet = bet
        self.econ = econ

    async def interaction_check(self, i):
        if i.user.id != self.author.id:
            await i.response.send_message("not your bet", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True

    @discord.ui.button(label="Yes, send it all", style=discord.ButtonStyle.danger)
    async def confirm(self, b, i):
        spin = random.randint(0, 36)
        color = roulette_color(spin)
        won = self.bet * 2 if self.choice == color else 0

        econ = loadEcon(econ_file_for(self.guild))  # reload, balance mighta changed since the confirm popped up
        a = acct(econ, self.guild, self.author)
        a["bal"] = max(0, a["bal"] - self.bet) + won
        save(econ, econ_file_for(self.guild))

        for c in self.children:
            c.disabled = True
        net = won - self.bet
        result = f"ball landed on {spin} ({color}) - " + (f"YOU WON {chips(won)}" if net >= 0 else f"lost it all, -{chips(self.bet)}")
        await i.response.edit_message(content=f"{result}\nbal: {chips(a['bal'])}", view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, b, i):
        for c in self.children:
            c.disabled = True
        await i.response.edit_message(content="chickened out, bet cancelled", view=self)


async def do_allin(ctx, choice):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    choice = choice.lower().strip()
    if choice not in ("red", "black"):
        await reply(ctx, content="allin only does red or black", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a["bal"] <= 0:
        await reply(ctx, content="you're broke, nothing to bet", ephemeral=True)
        return
    view = AllInConfirm(ctx.author, space, choice, a["bal"], econ)
    await reply(ctx, content=f"betting your entire stack ({chips(a['bal'])}) on **{choice}**. you sure?", view=view)


@bot.slash_command(name="allin", description="bet your entire balance on red or black")
async def allin(ctx, choice: Option(str, "red or black", choices=["red", "black"])):
    await do_allin(ctx, choice)


@bot.command(name="allin")
async def allin_cmd(ctx, choice: str):
    await do_allin(ctx, choice)


# ================= WAR =================
# simplest game in the file on purpose - one card each, higher wins, tie is
# a push (nobody wanted the classic double-down-on-tie escalation, too easy
# to accidentally bet more than you meant to)
def card_disp(n):
    return {11: "J", 12: "Q", 13: "K", 14: "A"}.get(n, str(n))


async def do_war(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return

    rig = pop_rig(space, ctx.author)
    if rig == "win":
        mine, dealer = 14, random.randint(2, 13)
    elif rig == "lose":
        mine, dealer = random.randint(2, 13), 14
    else:
        mine, dealer = random.randint(2, 14), random.randint(2, 14)
    if mine == dealer:
        msg = f"you drew {card_disp(mine)}, dealer drew {card_disp(dealer)} - tie, push, bet returned untouched"
    elif mine > dealer:
        a["bal"] += amount
        msg = f"you drew {card_disp(mine)}, dealer drew {card_disp(dealer)} - you win {chips(amount)}"
    else:
        a["bal"] -= amount
        msg = f"you drew {card_disp(mine)}, dealer drew {card_disp(dealer)} - dealer wins, lost {chips(amount)}"
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="war", description="one card each, highest wins, tie is a push")
async def war(ctx, amount: Option(int, "bet", min_value=1)):
    await do_war(ctx, amount)


@bot.command(name="war")
async def war_cmd(ctx, amount: int):
    await do_war(ctx, amount)


# ================= HIGHER/LOWER =================
class HiLo(discord.ui.View):
    """guess if the next card's higher or lower, pot grows 1.8x each correct
    guess, cash out whenever or bust and lose the original bet (not the pot -
    you never actually banked the pot's growth til you hit cash out)"""

    def __init__(self, author, guild, econ, amount, current):
        super().__init__(timeout=45)
        self.author = author
        self.guild = guild
        self.econ = econ
        self.amount = amount
        self.pot = amount
        self.current = current

    async def interaction_check(self, i):
        if i.user.id != self.author.id:
            await i.response.send_message("not your game", ephemeral=True)
            return False
        return True

    def show(self):
        return f"pot: {chips(self.pot)}\ncurrent card: {card_disp(self.current)}\nhigher, lower, or cash out?"

    async def guess(self, i, direction):
        new = random.randint(2, 14)
        if new == self.current:
            await i.response.edit_message(content=self.show() + f"\n\ntied on {card_disp(new)} - push, guess again", view=self)
            return
        correct = (direction == "higher") == (new > self.current)
        self.current = new
        if correct:
            self.pot = int(self.pot * 1.8)
            await i.response.edit_message(content=self.show(), view=self)
        else:
            a = acct(self.econ, self.guild, self.author)
            a["bal"] -= self.amount
            save(self.econ, econ_file_for(self.guild))
            for c in self.children:
                c.disabled = True
            await i.response.edit_message(content=self.show() + f"\n\nwrong, lost {chips(self.amount)}. bal {chips(a['bal'])}", view=self)

    @discord.ui.button(label="Higher", style=discord.ButtonStyle.success)
    async def higher(self, b, i):
        await self.guess(i, "higher")

    @discord.ui.button(label="Lower", style=discord.ButtonStyle.danger)
    async def lower(self, b, i):
        await self.guess(i, "lower")

    @discord.ui.button(label="Cash Out", style=discord.ButtonStyle.secondary)
    async def cashout(self, b, i):
        net = self.pot - self.amount
        a = acct(self.econ, self.guild, self.author)
        a["bal"] += net
        save(self.econ, econ_file_for(self.guild))
        for c in self.children:
            c.disabled = True
        sign = "+" if net >= 0 else ""
        await i.response.edit_message(content=f"cashed out {chips(self.pot)} ({sign}{net} net). bal {chips(a['bal'])}", view=self)


async def do_hilo(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    first = random.randint(2, 14)
    view = HiLo(ctx.author, space, econ, amount, first)
    await reply(ctx, content=view.show(), view=view)


@bot.slash_command(name="hilo", description="guess higher or lower, pot grows each correct guess")
async def hilo(ctx, amount: Option(int, "bet", min_value=1)):
    await do_hilo(ctx, amount)


@bot.command(name="hilo")
async def hilo_cmd(ctx, amount: int):
    await do_hilo(ctx, amount)


# ================= CRASH =================
# real crash games tick a multiplier live and you smash a cash-out button
# before it pops - doing that with discord message edits every half second
# gets rate-limited fast on a small bot, so this is the "call your shot"
# version instead: pick your cash-out target up front, the game rolls where
# it actually would've crashed, and you win if the crash point clears it
def crash_roll():
    r = random.random()
    cp = max(1.0, round(0.97 / (1 - r), 2))  # ~3% house edge baked into the curve
    return min(cp, 1000.0)


async def do_crash(ctx, amount, target):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    if target < 1.01:
        await reply(ctx, content="target has to be at least 1.01x", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return

    crash_point = crash_roll()
    if crash_point >= target:
        payout = int(amount * target)
        a["bal"] += payout - amount
        msg = f"crashed at {crash_point}x - your {target}x cashed out clean, won {chips(payout - amount)}"
    else:
        a["bal"] -= amount
        msg = f"crashed at {crash_point}x, before your {target}x target - lost {chips(amount)}"
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="crash", description="pick a cash-out multiplier, see if the crash clears it")
async def crash(ctx, amount: Option(int, "bet", min_value=1), target: Option(float, "cash-out multiplier, e.g. 2.5", min_value=1.01)):
    await do_crash(ctx, amount, target)


@bot.command(name="crash")
async def crash_cmd(ctx, amount: int, target: float):
    await do_crash(ctx, amount, target)


# ================= BLACKJACK =================
def cardval():
    return random.choice([2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 10, 10, 11])


def handtotal(h):
    t = sum(h)
    aces = h.count(11)
    while t > 21 and aces > 0:
        t -= 10
        aces -= 1
    return t


class BJ(discord.ui.View):
    def __init__(self, author, guild, bet, me, dealer, econ):
        super().__init__(timeout=60)
        self.author = author
        self.guild = guild
        self.bet = bet
        self.me = me
        self.dealer = dealer
        self.econ = econ
        self.can_double = True  # only allowed as your very first move

    async def interaction_check(self, i):
        if i.user.id != self.author.id:
            await i.response.send_message("not your game", ephemeral=True)
            return False
        return True

    def show(self, reveal=False):
        if reveal:
            dside = " ".join(str(c) for c in self.dealer) + f" ({handtotal(self.dealer)})"
        else:
            dside = f"{self.dealer[0]} ?"
        return f"you: {' '.join(str(c) for c in self.me)} ({handtotal(self.me)})\ndealer: {dside}\nbet: {chips(self.bet)}"

    async def payout(self, i, msg, delta):
        a = acct(self.econ, self.guild, self.author)
        a["bal"] += delta
        save(self.econ, econ_file_for(self.guild))
        for c in self.children:
            c.disabled = True
        await i.response.edit_message(content=self.show(True) + f"\n\n{msg}, bal {chips(a['bal'])}", view=self)

    @discord.ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, b, i):
        self.can_double = False
        self.me.append(cardval())
        if handtotal(self.me) > 21:
            await self.payout(i, "bust, you lose", -self.bet)
            return
        await i.response.edit_message(content=self.show(), view=self)

    @discord.ui.button(label="Stand", style=discord.ButtonStyle.secondary)
    async def stand(self, b, i):
        while handtotal(self.dealer) < 17:
            self.dealer.append(cardval())
        p, d = handtotal(self.me), handtotal(self.dealer)
        if d > 21 or p > d:
            await self.payout(i, "you win", self.bet)
        elif p == d:
            await self.payout(i, "push", 0)
        else:
            await self.payout(i, "you lose", -self.bet)

    @discord.ui.button(label="Double Down", style=discord.ButtonStyle.success)
    async def double(self, b, i):
        if not self.can_double:
            await i.response.send_message("only good as your first move, before you've hit", ephemeral=True)
            return
        a = acct(self.econ, self.guild, self.author)
        if a["bal"] < self.bet:
            await i.response.send_message("not enough chips to double this bet", ephemeral=True)
            return
        self.can_double = False
        self.bet *= 2
        self.me.append(cardval())
        if handtotal(self.me) > 21:
            await self.payout(i, "bust on the double, you lose", -self.bet)
            return
        while handtotal(self.dealer) < 17:
            self.dealer.append(cardval())
        p, d = handtotal(self.me), handtotal(self.dealer)
        if d > 21 or p > d:
            await self.payout(i, "you win (doubled)", self.bet)
        elif p == d:
            await self.payout(i, "push (doubled)", 0)
        else:
            await self.payout(i, "you lose (doubled)", -self.bet)


async def do_blackjack(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return

    me = [cardval(), cardval()]
    dealer = [cardval(), cardval()]
    v = BJ(ctx.author, space, amount, me, dealer, econ)

    if handtotal(me) == 21:
        payout = int(amount * 1.5)
        a["bal"] += payout
        save(econ, econ_file_for(space))
        for c in v.children:
            c.disabled = True
        await reply(ctx, content=f"blackjack!\n{v.show(True)}\n\nwon {chips(payout)}, bal {chips(a['bal'])}", view=v)
        return

    await reply(ctx, content=v.show(), view=v)


@bot.slash_command(name="blackjack", description="hit or stand vs the dealer")
async def blackjack(ctx, amount: Option(int, "bet", min_value=1)):
    await do_blackjack(ctx, amount)


@bot.command(name="blackjack", aliases=["bj"])
async def blackjack_cmd(ctx, amount: int):
    await do_blackjack(ctx, amount)


# ================= DUEL =================
# direct player-vs-player wager, decided by a coinflip once both sides
# confirm. requester's chips aren't touched until acceptance so nobody can
# challenge someone into a bet they can't cover
class DuelConfirm(discord.ui.View):
    def __init__(self, challenger, opponent, guild, amount):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent = opponent
        self.guild = guild
        self.amount = amount

    async def interaction_check(self, i):
        if i.user.id != self.opponent.id:
            await i.response.send_message("this challenge isnt for you", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, b, i):
        econ = loadEcon(econ_file_for(self.guild))
        chal = acct(econ, self.guild, self.challenger)
        opp = acct(econ, self.guild, self.opponent)
        if chal["bal"] < self.amount or opp["bal"] < self.amount:
            for c in self.children:
                c.disabled = True
            await i.response.edit_message(content="one of you doesn't have enough chips anymore, duel cancelled", view=self)
            return
        winner = random.choice([self.challenger, self.opponent])
        loser = self.opponent if winner.id == self.challenger.id else self.challenger
        wa = acct(econ, self.guild, winner)
        la = acct(econ, self.guild, loser)
        wa["bal"] += self.amount
        la["bal"] -= self.amount
        save(econ, econ_file_for(self.guild))
        for c in self.children:
            c.disabled = True
        await i.response.edit_message(content=f"⚔️ {winner.mention} wins the duel and takes {chips(self.amount)} from {loser.mention}", view=self)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary)
    async def decline(self, b, i):
        for c in self.children:
            c.disabled = True
        await i.response.edit_message(content="duel declined", view=self)


async def do_duel(ctx, opponent, amount):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if opponent.id == ctx.author.id:
        await reply(ctx, content="cant duel yourself", ephemeral=True)
        return
    if opponent.bot:
        await reply(ctx, content="bots dont duel", ephemeral=True)
        return
    if is_banned(ctx.author, ctx.guild):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon()
    a = acct(econ, ctx.guild, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad wager amount", ephemeral=True)
        return
    view = DuelConfirm(ctx.author, opponent, ctx.guild, amount)
    await reply(ctx, content=f"{opponent.mention}, {ctx.author.mention} is challenging you to a {chips(amount)} duel - coinflip decides. accept?", view=view)


@bot.slash_command(name="duel", description="challenge someone to a coinflip wager")
async def duel(ctx, user: Option(discord.Member, "who to challenge"), amount: Option(int, "wager", min_value=1)):
    await do_duel(ctx, user, amount)


@bot.command(name="duel")
async def duel_cmd(ctx, user: discord.Member, amount: int):
    await do_duel(ctx, user, amount)


# ================= SCRATCH TICKET =================
SCRATCH_PRICE = 100
SCRATCH_PRIZES = [0, 50, 100, 150, 250, 500, 1000]
SCRATCH_WEIGHTS = [45, 25, 15, 8, 4, 2, 1]


async def do_scratch(ctx):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if a["bal"] < SCRATCH_PRICE:
        await reply(ctx, content=f"tickets cost {chips(SCRATCH_PRICE)}, you don't have enough", ephemeral=True)
        return
    prize = random.choices(SCRATCH_PRIZES, weights=SCRATCH_WEIGHTS, k=1)[0]
    a["bal"] = a["bal"] - SCRATCH_PRICE + prize
    save(econ, econ_file_for(space))
    result = f"won {chips(prize)}!" if prize > 0 else "nothing this time"
    await reply(ctx, content=f"🎟️ scratched a {chips(SCRATCH_PRICE)} ticket... {result} bal {chips(a['bal'])}")


@bot.slash_command(name="scratch", description=f"buy a {SCRATCH_PRICE}-chip scratch ticket, instant reveal")
async def scratch(ctx):
    await do_scratch(ctx)


@bot.command(name="scratch")
async def scratch_cmd(ctx):
    await do_scratch(ctx)


# ================= HORSE RACING =================
HORSES = [
    {"name": "Thunderbolt", "odds": 2},
    {"name": "Lucky Star", "odds": 3},
    {"name": "Midnight Runner", "odds": 4},
    {"name": "Golden Hoof", "odds": 6},
]


async def do_horserace(ctx, amount, horse_name):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    pick = next((h for h in HORSES if h["name"].lower() == horse_name.lower().strip()), None)
    if not pick:
        names = ", ".join(h["name"] for h in HORSES)
        await reply(ctx, content=f"pick one of: {names}", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    winner = random.choice(HORSES)
    if winner["name"] == pick["name"]:
        payout = amount * pick["odds"]
        a["bal"] += payout - amount
        msg = f"🏇 {winner['name']} wins! you called it at {pick['odds']}x, +{chips(payout - amount)}"
    else:
        a["bal"] -= amount
        msg = f"🏇 {winner['name']} wins - you backed {pick['name']}, lost {chips(amount)}"
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="horserace", description="bet on a horse, payout scales with its odds")
async def horserace(ctx, amount: Option(int, "bet", min_value=1), horse: Option(str, "which horse", choices=[h["name"] for h in HORSES])):
    await do_horserace(ctx, amount, horse)


@bot.command(name="horserace", aliases=["horse"])
async def horserace_cmd(ctx, amount: int, *, horse: str):
    await do_horserace(ctx, amount, horse)


# ================= PLINKO =================
# drop a chip through 8 rows of pegs, it lands in one of 9 slots - edges
# pay big, middle barely breaks even, matches the classic plinko shape
PLINKO_ROWS = 8
PLINKO_MULTS = [5.6, 2.1, 1.4, 0.6, 0.3, 0.6, 1.4, 2.1, 5.6]


async def do_plinko(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    path = [random.randint(0, 1) for _ in range(PLINKO_ROWS)]
    bucket = sum(path)
    mult = PLINKO_MULTS[bucket]
    payout = int(amount * mult)
    a["bal"] += payout - amount
    save(econ, econ_file_for(space))
    slots_row = ["·"] * len(PLINKO_MULTS)
    slots_row[bucket] = "●"
    await reply(ctx, content=f"[ {' '.join(slots_row)} ]\nlanded in the {mult}x slot - {'won' if payout>=amount else 'lost'} {chips(abs(payout - amount))}. bal {chips(a['bal'])}")


@bot.slash_command(name="plinko", description="drop a chip through the pegs")
async def plinko(ctx, amount: Option(int, "bet", min_value=1)):
    await do_plinko(ctx, amount)


@bot.command(name="plinko")
async def plinko_cmd(ctx, amount: int):
    await do_plinko(ctx, amount)


# ================= MINES =================
# 20-tile grid (4 rows of 5, 5th row reserved for cash out - discord caps
# views at 25 components). pick tiles, each safe one bumps the multiplier,
# hit a mine and the bet's gone. multiplier math approximates fair odds for
# the chosen mine count then shaves a little off per step for house edge
MINES_TOTAL = 20


def mines_multiplier(mine_count, revealed):
    mult = 1.0
    for i in range(revealed):
        remaining_total = MINES_TOTAL - i
        remaining_safe = (MINES_TOTAL - mine_count) - i
        if remaining_safe <= 0:
            break
        mult *= remaining_total / remaining_safe
    return mult * (0.95 ** revealed)


class MinesTile(discord.ui.Button):
    def __init__(self, idx):
        super().__init__(style=discord.ButtonStyle.secondary, label="❔", row=idx // 5)
        self.idx = idx

    async def callback(self, interaction):
        view: MinesGame = self.view
        if interaction.user.id != view.author.id:
            await interaction.response.send_message("not your game", ephemeral=True)
            return
        if view.over or self.idx in view.revealed:
            await interaction.response.defer()
            return
        if self.idx in view.mine_positions:
            view.over = True
            for child in view.children:
                if isinstance(child, MinesTile) and child.idx in view.mine_positions:
                    child.label = "💣"
                    child.style = discord.ButtonStyle.danger
                child.disabled = True
            a = acct(view.econ, view.guild, view.author)
            a["bal"] -= view.amount
            save(view.econ, econ_file_for(view.guild))
            await interaction.response.edit_message(content=f"💥 hit a mine! lost {chips(view.amount)}. bal {chips(a['bal'])}", view=view)
            return
        view.revealed.add(self.idx)
        self.label = "💎"
        self.style = discord.ButtonStyle.success
        self.disabled = True
        view.multiplier = mines_multiplier(view.mine_count, len(view.revealed))
        view.update_cashout_label()
        if len(view.revealed) >= MINES_TOTAL - view.mine_count:
            view.over = True
            for child in view.children:
                child.disabled = True
            a = acct(view.econ, view.guild, view.author)
            payout = int(view.amount * view.multiplier)
            a["bal"] += payout - view.amount
            save(view.econ, econ_file_for(view.guild))
            await interaction.response.edit_message(
                content=f"cleared the whole board! {view.multiplier:.2f}x - won {chips(payout - view.amount)}. bal {chips(a['bal'])}",
                view=view,
            )
            return
        await interaction.response.edit_message(content=view.show(), view=view)


class MinesCashout(discord.ui.Button):
    def __init__(self):
        super().__init__(style=discord.ButtonStyle.primary, label="Cash Out", row=4)

    async def callback(self, interaction):
        view: MinesGame = self.view
        if interaction.user.id != view.author.id:
            await interaction.response.send_message("not your game", ephemeral=True)
            return
        if view.over or not view.revealed:
            await interaction.response.defer()
            return
        view.over = True
        for child in view.children:
            child.disabled = True
        a = acct(view.econ, view.guild, view.author)
        payout = int(view.amount * view.multiplier)
        a["bal"] += payout - view.amount
        save(view.econ, econ_file_for(view.guild))
        await interaction.response.edit_message(
            content=f"cashed out at {view.multiplier:.2f}x - won {chips(payout - view.amount)}. bal {chips(a['bal'])}",
            view=view,
        )


class MinesGame(discord.ui.View):
    def __init__(self, author, guild, econ, amount, mine_count):
        super().__init__(timeout=120)
        self.author = author
        self.guild = guild
        self.econ = econ
        self.amount = amount
        self.mine_count = mine_count
        self.mine_positions = set(random.sample(range(MINES_TOTAL), mine_count))
        self.revealed = set()
        self.over = False
        self.multiplier = 1.0
        for i in range(MINES_TOTAL):
            self.add_item(MinesTile(i))
        self.cashout_btn = MinesCashout()
        self.add_item(self.cashout_btn)

    def update_cashout_label(self):
        self.cashout_btn.label = f"Cash Out ({self.multiplier:.2f}x)"

    def show(self):
        return f"bet {chips(self.amount)} | {self.mine_count} mines | multiplier {self.multiplier:.2f}x\npick a tile or cash out"


async def do_mines(ctx, amount, mine_count):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    if mine_count < 1 or mine_count > MINES_TOTAL - 1:
        await reply(ctx, content=f"mines has to be 1-{MINES_TOTAL - 1}", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    view = MinesGame(ctx.author, space, econ, amount, mine_count)
    await reply(ctx, content=view.show(), view=view)


@bot.slash_command(name="mines", description="pick safe tiles, avoid mines, cash out anytime")
async def mines(ctx, amount: Option(int, "bet", min_value=1), mine_count: Option(int, "how many mines (1-19)", min_value=1, max_value=MINES_TOTAL - 1) = 5):
    await do_mines(ctx, amount, mine_count)


@bot.command(name="mines")
async def mines_cmd(ctx, amount: int, mine_count: int = 5):
    await do_mines(ctx, amount, mine_count)


# ================= WHEEL =================
# single spin, weighted like the slots reel weights up top - the fat
# 0x/0.5x/1x wedges show up way more than the skinny 10x one
WHEEL_SEGMENTS = [(0, 30), (0.5, 22), (1, 20), (1.5, 14), (2, 8), (3, 4), (5, 1.5), (10, 0.5)]


async def do_wheel(ctx, amount):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    mult = random.choices([s[0] for s in WHEEL_SEGMENTS], weights=[s[1] for s in WHEEL_SEGMENTS], k=1)[0]
    payout = int(amount * mult)
    bal_before = a["bal"]
    a["bal"] = a["bal"] - amount + payout
    net = payout - amount
    check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"🎡 landed on **{mult}x** - {'won' if net >= 0 else 'lost'} {chips(abs(net))}. bal {chips(a['bal'])}")


@bot.slash_command(name="wheel", description="spin the wheel, multiplier ranges from 0x to 10x")
async def wheel(ctx, amount: Option(int, "bet", min_value=1)):
    await do_wheel(ctx, amount)


@bot.command(name="wheel")
async def wheel_cmd(ctx, amount: int):
    await do_wheel(ctx, amount)


# ================= KENO =================
# pick 1-5 numbers from 1-40, house draws 10 - payout table is keyed by how
# many you picked since fewer picks means each match is rarer and worth more
KENO_RANGE = 40
KENO_DRAWN = 10
KENO_PAYTABLE = {
    1: {1: 3},
    2: {1: 1, 2: 6},
    3: {2: 2, 3: 12},
    4: {2: 1, 3: 6, 4: 25},
    5: {2: 1, 3: 3, 4: 15, 5: 50},
}


def parse_keno_picks(raw):
    parts = raw.replace(",", " ").split()
    nums = []
    for p in parts:
        if not p.isdigit():
            return None
        n = int(p)
        if n < 1 or n > KENO_RANGE or n in nums:
            return None
        nums.append(n)
    if not (1 <= len(nums) <= 5):
        return None
    return nums


async def do_keno(ctx, amount, raw_numbers):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    picks = parse_keno_picks(raw_numbers)
    if not picks:
        await reply(ctx, content=f"pick 1-5 unique numbers between 1 and {KENO_RANGE}, space separated - e.g. `4 15 22`", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    drawn = random.sample(range(1, KENO_RANGE + 1), KENO_DRAWN)
    matched = sorted(set(picks) & set(drawn))
    mult = KENO_PAYTABLE[len(picks)].get(len(matched), 0)
    payout = int(amount * mult)
    bal_before = a["bal"]
    a["bal"] = a["bal"] - amount + payout
    net = payout - amount
    check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    drawn_disp = ", ".join(str(n) for n in sorted(drawn))
    match_note = f"matched {len(matched)}/{len(picks)}" + (f" ({', '.join(str(m) for m in matched)})" if matched else "")
    await reply(ctx, content=f"drawn: {drawn_disp}\n{match_note} - {'won' if net >= 0 else 'lost'} {chips(abs(net))}. bal {chips(a['bal'])}")


@bot.slash_command(name="keno", description="pick 1-5 numbers (1-40), match the draw for a payout")
async def keno(ctx, amount: Option(int, "bet", min_value=1), numbers: Option(str, "your picks, space separated e.g. '4 15 22'")):
    await do_keno(ctx, amount, numbers)


@bot.command(name="keno")
async def keno_cmd(ctx, amount: int, *, numbers: str):
    await do_keno(ctx, amount, numbers)


# ================= BACCARAT =================
# simplified - no card totals, just weighted to roughly match real baccarat
# outcome odds (banker slightly favored, tie is rare)
async def do_baccarat(ctx, amount, choice):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    choice = choice.lower().strip()
    if choice not in ("player", "banker", "tie"):
        await reply(ctx, content="bet player, banker, or tie", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    r = random.random()
    outcome = "banker" if r < 0.4586 else ("player" if r < 0.9048 else "tie")
    bal_before = a["bal"]
    net = 0
    if outcome == "tie" and choice == "tie":
        payout = amount * 9  # 8:1 plus stake back
        a["bal"] += payout - amount
        net = payout - amount
        msg = f"tie! 8:1 payout, won {chips(net)}"
    elif outcome == "tie":
        msg = f"tie - your {choice} bet pushes, nothing won or lost"
    elif outcome == choice == "banker":
        won = int(amount * 0.95)  # 5% commission, standard baccarat house rule
        a["bal"] += won
        net = won
        msg = f"banker wins, you called it - won {chips(won)} (5% house commission)"
    elif outcome == choice == "player":
        a["bal"] += amount
        net = amount
        msg = f"player wins, you called it - won {chips(amount)}"
    else:
        a["bal"] -= amount
        net = -amount
        msg = f"{outcome} wins, not what you bet - lost {chips(amount)}"
    if net != 0:
        check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="baccarat", description="bet on player, banker, or tie")
async def baccarat(ctx, amount: Option(int, "bet", min_value=1), choice: Option(str, "pick one", choices=["player", "banker", "tie"])):
    await do_baccarat(ctx, amount, choice)


@bot.command(name="baccarat", aliases=["bacc"])
async def baccarat_cmd(ctx, amount: int, choice: str):
    await do_baccarat(ctx, amount, choice)


# ================= ROCK PAPER SCISSORS =================
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


async def do_rps(ctx, amount, choice):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    choice = choice.lower().strip()
    if choice not in RPS_BEATS:
        await reply(ctx, content="pick rock, paper, or scissors", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    house = random.choice(list(RPS_BEATS.keys()))
    bal_before = a["bal"]
    if house == choice:
        msg = f"house also threw {house} - push, bet returned"
        net = 0
    elif RPS_BEATS[choice] == house:
        a["bal"] += amount
        msg = f"you threw {choice}, house threw {house} - you win! +{chips(amount)}"
        net = amount
    else:
        a["bal"] -= amount
        msg = f"you threw {choice}, house threw {house} - you lose, -{chips(amount)}"
        net = -amount
    if net != 0:
        check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="rps", description="rock paper scissors vs the house, win doubles your bet")
async def rps(ctx, amount: Option(int, "bet", min_value=1), choice: Option(str, "pick one", choices=["rock", "paper", "scissors"])):
    await do_rps(ctx, amount, choice)


@bot.command(name="rps")
async def rps_cmd(ctx, amount: int, choice: str):
    await do_rps(ctx, amount, choice)


# ================= LADDER =================
# pick how many rungs to climb up front (same "call your shot" approach as
# crash instead of a live cash-out button) - each rung has a house-edged
# survive chance, clear every rung for 2x/rung, miss one and lose it all
LADDER_SURVIVE_CHANCE = 0.47  # < 50% so 2x-per-rung still favors the house


async def do_ladder(ctx, amount, rungs):
    space = get_space(ctx)
    if is_banned(ctx.author, space):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    if rungs < 1 or rungs > 6:
        await reply(ctx, content="pick 1-6 rungs", ephemeral=True)
        return
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, ctx.author)
    if amount <= 0 or amount > a["bal"]:
        await reply(ctx, content="bad bet amount", ephemeral=True)
        return
    cleared = 0
    for _ in range(rungs):
        if random.random() < LADDER_SURVIVE_CHANCE:
            cleared += 1
        else:
            break
    bal_before = a["bal"]
    if cleared == rungs:
        payout = amount * (2 ** rungs)
        a["bal"] += payout - amount
        net = payout - amount
        msg = f"climbed all {rungs} rungs! {2 ** rungs}x payout, won {chips(net)}"
    else:
        a["bal"] -= amount
        net = -amount
        msg = f"fell at rung {cleared + 1}/{rungs} - lost {chips(amount)}"
    check_bet_badges(a, space.name, amount, bal_before, net)
    save(econ, econ_file_for(space))
    await reply(ctx, content=f"{msg}. bal {chips(a['bal'])}")


@bot.slash_command(name="ladder", description="pick 1-6 rungs, each one doubles your bet, miss one and lose it all")
async def ladder(ctx, amount: Option(int, "bet", min_value=1), rungs: Option(int, "how many rungs to attempt (1-6)", min_value=1, max_value=6)):
    await do_ladder(ctx, amount, rungs)


@bot.command(name="ladder")
async def ladder_cmd(ctx, amount: int, rungs: int):
    await do_ladder(ctx, amount, rungs)


# ================= LOTTERY =================
# daily drawing per server - ticket purchases pool into a pot, one ticket is
# drawn at random (weighted by how many you bought) and takes the whole pot.
# runs on the same kind of hourly-check background loop as the weekly
# tournament above.
LOTTERY_TICKET_PRICE = 100
LOTTERY_PERIOD = 86400  # 24h


async def do_lottery_status(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    g = get_guild_cfg(loadCfg(), ctx.guild)
    lot = g["lottery"]
    total_tickets = sum(lot["tickets"].values())
    yours = lot["tickets"].get(str(ctx.author.id), 0)
    left = LOTTERY_PERIOD - (time.time() - lot["period_start"])
    h = max(0, int(left / 3600))
    await reply(ctx, content=(
        f"🎟️ pot: {chips(lot['pot'])}\n"
        f"tickets sold: {total_tickets} (yours: {yours})\n"
        f"ticket price: {chips(LOTTERY_TICKET_PRICE)} each - `/buyticket <count>`\n"
        f"next draw in ~{h}h"
    ))


@bot.slash_command(name="lottery", description="see the current lottery pot and how many tickets are in")
async def lottery(ctx):
    await do_lottery_status(ctx)


@bot.command(name="lottery")
async def lottery_cmd(ctx):
    await do_lottery_status(ctx)


async def do_buyticket(ctx, count):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if is_banned(ctx.author, ctx.guild):
        await reply(ctx, content="you're banned from gambling here", ephemeral=True)
        return
    if count < 1:
        await reply(ctx, content="buy at least 1 ticket", ephemeral=True)
        return
    cost = count * LOTTERY_TICKET_PRICE
    econ = loadEcon()
    a = acct(econ, ctx.guild, ctx.author)
    if cost > a["bal"]:
        await reply(ctx, content=f"{count} tickets costs {chips(cost)}, you don't have that", ephemeral=True)
        return
    a["bal"] -= cost
    save(econ)
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    lot = g["lottery"]
    lot["pot"] += cost
    uid = str(ctx.author.id)
    lot["tickets"][uid] = lot["tickets"].get(uid, 0) + count
    saveCfg(all_cfg)
    log_event(ctx.guild.name, f"{ctx.author.display_name} bought {count} lottery ticket(s)")
    await reply(ctx, content=f"bought {count} ticket(s) for {chips(cost)}. pot is now {chips(lot['pot'])}. bal {chips(a['bal'])}")


@bot.slash_command(name="buyticket", description="buy lottery tickets, more tickets = better odds")
async def buyticket(ctx, count: Option(int, "how many", min_value=1)):
    await do_buyticket(ctx, count)


@bot.command(name="buyticket", aliases=["ticket"])
async def buyticket_cmd(ctx, count: int):
    await do_buyticket(ctx, count)


@tasks.loop(hours=1)
async def lottery_draw_loop():
    all_cfg = loadCfg()
    changed = False
    for gid, g in list(all_cfg.items()):
        if gid == PERSONAL_SPACE.id:
            continue  # no guild-wide lottery for the personal/DM space
        lot = g.setdefault("lottery", {"pot": 0, "tickets": {}, "period_start": time.time()})
        if time.time() - lot.get("period_start", time.time()) < LOTTERY_PERIOD:
            continue
        guild = bot.get_guild(int(gid))
        tickets = lot.get("tickets", {})
        if guild and tickets and lot.get("pot", 0) > 0:
            pool = []
            for uid, n in tickets.items():
                pool.extend([uid] * n)
            winner_id = random.choice(pool)
            member = guild.get_member(int(winner_id))
            if member:
                econ = loadEcon()
                a = acct(econ, guild, member)
                a["bal"] += lot["pot"]
                save(econ)
                log_event(guild.name, f"{member.display_name} won the {chips(lot['pot'])} lottery ({tickets[winner_id]} ticket(s) in)")
                channel_id = g.get("announce_channel")
                if channel_id:
                    channel = guild.get_channel(channel_id)
                    if channel:
                        try:
                            await channel.send(f"🎟️ **lottery drawn!** {member.mention} won {chips(lot['pot'])}!")
                        except discord.Forbidden:
                            pass
        g["lottery"] = {"pot": 0, "tickets": {}, "period_start": time.time()}
        changed = True
    if changed:
        saveCfg(all_cfg)


@lottery_draw_loop.before_loop
async def before_lottery_draw_loop():
    await bot.wait_until_ready()


# ================= CASINO MANAGER STUFF =================
class ManagerPicker(discord.ui.View):
    """dropdown menu so the owner doesn't have to type an id or role name,
    just pick from the list - or hit the button to spin up a dedicated
    'Casino Staff' role from scratch"""

    def __init__(self, author):
        super().__init__(timeout=60)
        self.author = author

    async def interaction_check(self, i):
        if i.user.id != self.author.id:
            await i.response.send_message("this menu isnt for you", ephemeral=True)
            return False
        return True

    @discord.ui.select(select_type=discord.ComponentType.user_select, placeholder="pick a user as casino manager")
    async def pick_user(self, select, interaction):
        all_cfg = loadCfg()
        g = get_guild_cfg(all_cfg, interaction.guild)
        picked = select.values[0]
        if picked.id not in g["managers"]:
            g["managers"].append(picked.id)
        saveCfg(all_cfg)
        log_event(interaction.guild.name, f"{picked.display_name} was made a casino manager")
        await interaction.response.edit_message(content=f"{picked.mention} is now a casino manager", view=None)

    @discord.ui.select(select_type=discord.ComponentType.role_select, placeholder="or pick an existing role instead")
    async def pick_role(self, select, interaction):
        all_cfg = loadCfg()
        g = get_guild_cfg(all_cfg, interaction.guild)
        picked = select.values[0]
        g["manager_role"] = picked.id
        saveCfg(all_cfg)
        log_event(interaction.guild.name, f"role '{picked.name}' was made a casino manager role")
        await interaction.response.edit_message(content=f"anyone with {picked.mention} is now a casino manager", view=None)

    @discord.ui.button(label="Create 'Casino Staff' Role", style=discord.ButtonStyle.success)
    async def create_role(self, b, interaction):
        existing = discord.utils.get(interaction.guild.roles, name="Casino Staff")
        try:
            role = existing or await interaction.guild.create_role(
                name="Casino Staff", color=discord.Color.gold(), reason="casino bot setup"
            )
        except discord.Forbidden:
            await interaction.response.send_message("i don't have permission to create roles here - give me Manage Roles", ephemeral=True)
            return
        all_cfg = loadCfg()
        g = get_guild_cfg(all_cfg, interaction.guild)
        g["manager_role"] = role.id
        saveCfg(all_cfg)
        log_event(interaction.guild.name, f"'Casino Staff' role {'reused' if existing else 'created'} and set as the manager role")
        await interaction.response.edit_message(
            content=f"{'found and reused' if existing else 'created'} {role.mention} - assign it to whoever should manage the casino",
            view=None,
        )


@bot.slash_command(name="setmanager", description="owner only - pick who manages the casino")
async def setmanager(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_owner(ctx.author, ctx.guild):
        await reply(ctx, content="only the server owner can set casino managers", ephemeral=True)
        return
    await ctx.respond("pick a user, pick a role, or create a dedicated 'Casino Staff' role:", view=ManagerPicker(ctx.author))


@bot.command(name="setmanager")
async def setmanager_cmd(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_owner(ctx.author, ctx.guild):
        await reply(ctx, content="only the server owner can set casino managers", ephemeral=True)
        return
    await ctx.send("pick a user, pick a role, or create a dedicated 'Casino Staff' role:", view=ManagerPicker(ctx.author))


async def do_testmode(ctx):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature - it needs a shared server to make sense", ephemeral=True)
        return
    if not is_owner(ctx.author, ctx.guild):
        await reply(ctx, content="only the server owner can toggle testmode", ephemeral=True)
        return
    all_cfg = loadCfg()
    gcfg = get_guild_cfg(all_cfg, ctx.guild)
    gcfg["testmode"] = not gcfg.get("testmode", False)
    saveCfg(all_cfg)
    state = "ON" if gcfg["testmode"] else "off"
    log_event(ctx.guild.name, f"{ctx.author.display_name} turned testmode {state} - while on, the owner's own game bets always win (for checking payouts/embeds), everyone else stays normal random")
    await reply(ctx, content=f"testmode is now **{state}** - while on, your own coinflip/dice/slots/roulette/war bets always win so you can sanity-check payouts. everyone else's games are untouched.")


@bot.command(name="testmode")
async def testmode_cmd(ctx):
    await do_testmode(ctx)


async def do_setprefix(ctx, prefix):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    if len(prefix) > 3:
        await reply(ctx, content="keep it short, like ! ? or .", ephemeral=True)
        return
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    g["prefix"] = prefix
    saveCfg(all_cfg)
    await reply(ctx, content=f"prefix set to `{prefix}` (slash commands work no matter what this is set to)")


@bot.slash_command(name="setprefix", description="manager - change the text command prefix")
async def setprefix(ctx, prefix: Option(str, "like ! ? or .")):
    await do_setprefix(ctx, prefix)


@bot.command(name="setprefix")
async def setprefix_cmd(ctx, prefix: str):
    await do_setprefix(ctx, prefix)


async def do_addchips(ctx, user, amount):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    econ = loadEcon()
    a = acct(econ, ctx.guild, user)
    a["bal"] += amount
    save(econ)
    await reply(ctx, content=f"gave {user.mention} {chips(amount)}, new bal {chips(a['bal'])}")


@bot.slash_command(name="addchips", description="manager - add chips to someone")
async def addchips(ctx, user: Option(discord.Member, "who"), amount: Option(int, "how much", min_value=1)):
    await do_addchips(ctx, user, amount)


@bot.command(name="addchips")
async def addchips_cmd(ctx, user: discord.Member, amount: int):
    await do_addchips(ctx, user, amount)


async def do_removechips(ctx, user, amount):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    econ = loadEcon()
    a = acct(econ, ctx.guild, user)
    a["bal"] = max(0, a["bal"] - amount)
    save(econ)
    await reply(ctx, content=f"took {chips(amount)} from {user.mention}, new bal {chips(a['bal'])}")


@bot.slash_command(name="removechips", description="manager - take chips from someone")
async def removechips(ctx, user: Option(discord.Member, "who"), amount: Option(int, "how much", min_value=1)):
    await do_removechips(ctx, user, amount)


@bot.command(name="removechips")
async def removechips_cmd(ctx, user: discord.Member, amount: int):
    await do_removechips(ctx, user, amount)


async def do_ban(ctx, user, banned):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    if banned:
        if user.id not in g["banned"]:
            g["banned"].append(user.id)
        msg = f"{user.mention} is banned from gambling here"
    else:
        if user.id in g["banned"]:
            g["banned"].remove(user.id)
        msg = f"{user.mention} can gamble again"
    saveCfg(all_cfg)
    log_event(ctx.guild.name, f"{ctx.author.display_name} {'banned' if banned else 'unbanned'} {user.display_name} from gambling")
    await reply(ctx, content=msg)


@bot.slash_command(name="banuser", description="manager - block someone from the games")
async def banuser(ctx, user: Option(discord.Member, "who")):
    await do_ban(ctx, user, True)


@bot.command(name="banuser")
async def banuser_cmd(ctx, user: discord.Member):
    await do_ban(ctx, user, True)


@bot.slash_command(name="unbanuser", description="manager - let someone gamble again")
async def unbanuser(ctx, user: Option(discord.Member, "who")):
    await do_ban(ctx, user, False)


@bot.command(name="unbanuser")
async def unbanuser_cmd(ctx, user: discord.Member):
    await do_ban(ctx, user, False)


async def do_reset(ctx, target):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    econ = loadEcon()
    g = ensure_guild(econ, ctx.guild)
    g["users"][str(target.id)] = new_acct(target.display_name)
    save(econ)
    log_event(ctx.guild.name, f"{ctx.author.display_name} reset {target.display_name}'s account")
    await reply(ctx, content=f"reset {target.mention} to {chips(starting_bal)}")


@bot.slash_command(name="reset", description="manager - reset someones balance")
async def reset(ctx, user: Option(discord.Member, "who", required=False) = None):
    await do_reset(ctx, user or ctx.author)


@bot.command(name="reset")
async def reset_cmd(ctx, user: discord.Member = None):
    await do_reset(ctx, user or ctx.author)


async def do_setannounce(ctx, channel):
    if not need_guild(ctx):
        await reply(ctx, content="this is a server-only feature", ephemeral=True)
        return
    if not is_manager(ctx.author, ctx.guild):
        await reply(ctx, content="managers only", ephemeral=True)
        return
    all_cfg = loadCfg()
    g = get_guild_cfg(all_cfg, ctx.guild)
    g["announce_channel"] = channel.id
    saveCfg(all_cfg)
    await reply(ctx, content=f"weekly tournament results will post in {channel.mention}")


@bot.slash_command(name="setannounce", description="manager - set where weekly tournament results post")
async def setannounce(ctx, channel: Option(discord.TextChannel, "channel")):
    await do_setannounce(ctx, channel)


@bot.command(name="setannounce")
async def setannounce_cmd(ctx, channel: discord.TextChannel):
    await do_setannounce(ctx, channel)


# ================= STATS =================
async def do_stats(ctx, who):
    space = get_space(ctx)
    econ = loadEcon(econ_file_for(space))
    a = acct(econ, space, who)
    save(econ, econ_file_for(space))
    g = get_guild_cfg(loadCfg(), space)
    plays_this_week = g.get("week", {}).get("activity", {}).get(str(who.id), 0)
    desc = (
        f"balance: {chips(a['bal'])}\n"
        f"bank: {chips(a.get('bank', 0))}\n"
        f"daily streak: {a.get('daily_streak', 0)} days\n"
        f"win streak: {a.get('win_streak', 0)}\n"
        f"badges: {len(a.get('badges', []))}\n"
        f"games played this week: {plays_this_week}"
    )
    await reply(ctx, embed=discord.Embed(title=f"{who.display_name}'s stats", description=desc, color=discord.Color.blurple()))


@bot.slash_command(name="stats", description="see your (or someone's) casino stats")
async def stats(ctx, user: Option(discord.Member, "who", required=False) = None):
    await do_stats(ctx, user or ctx.author)


@bot.command(name="stats")
async def stats_cmd(ctx, user: discord.Member = None):
    await do_stats(ctx, user or ctx.author)


# ================= ERROR HANDLING =================
@bot.event
async def on_application_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.respond("admin only, nice try", ephemeral=True)
        return
    if isinstance(error, GameChannelLocked):
        return  # already told them which channel to use - nothing more to say
    print(error)
    try:
        await ctx.respond("something broke, check console", ephemeral=True)
    except discord.InteractionResponded:
        pass


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("admin only, nice try")
        return
    if isinstance(error, GameChannelLocked):
        return  # already told them which channel to use - nothing more to say
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"check your command args - {error}")
        return
    if isinstance(error, commands.CommandNotFound):
        return  # dont spam chat every time someone types a normal message that starts with the prefix char
    print(error)
    await ctx.send("something broke, check console")


if __name__ == "__main__":
    bot.run(TOKEN)