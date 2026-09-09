import csv
import io
import os
import time
from datetime import timedelta
from urllib.parse import urlencode

from pathlib import Path

from flask import Flask, Blueprint, jsonify, render_template, request, Response, session, redirect, url_for

try:
    from flask_session import Session as _ServerSession
except ImportError:  # pip install -r requirements.txt not run yet
    _ServerSession = None

import cog_utils as cu

from . import data as d
from . import discord_api as dapi
from .auth import (
    bp as auth_bp,
    login_required,
    bot_admin_required,
    guild_access_required,
    current_user,
    is_bot_admin,
    manageable_guild_ids,
)

app_routes = Blueprint("app_routes", __name__)


# ---------------------------------------------------------------- pages ----
@app_routes.route("/")
def landing():
    if current_user():
        return redirect(url_for("app_routes.dashboard_home"))
    return render_template(
        "landing.html",
        bot_name=os.getenv("BOT_NAME", "Gambly"),
        next=request.args.get("next", ""),
    )


@app_routes.route("/privacy")
def privacy():
    # public, no login required - this is the URL you paste into the
    # Discord Developer Portal's "Privacy Policy URL" field and into the
    # Privileged Intent request form.
    return render_template(
        "privacy.html",
        bot_name=os.getenv("BOT_NAME", "Gambly"),
        contact=os.getenv("PRIVACY_CONTACT", "").strip(),
    )


@app_routes.route("/tos")
def tos():
    # public, no login required - same idea as /privacy
    return render_template(
        "tos.html",
        bot_name=os.getenv("BOT_NAME", "Gambly"),
        contact=os.getenv("PRIVACY_CONTACT", "").strip(),
    )


@app_routes.route("/invite")
def invite():
    # Set this exact URL as the "Custom URL" under Install Link in the
    # Discord Developer Portal. Discord redirects here whenever someone
    # clicks "Add App" on your bot's profile, and this just forwards them
    # on to the real, guild-locked OAuth2 install URL below.
    return redirect(_invite_url())


@app_routes.route("/dashboard")
@login_required
def dashboard_home():
    bot_guilds = dapi.bot_fetch_my_guilds()
    mine = []
    for g in session.get("guilds") or []:
        if not dapi.can_manage(g):
            continue
        installed = g["id"] in bot_guilds
        mine.append({
            "id": g["id"],
            "name": g["name"],
            "icon": dapi.guild_icon_url(g["id"], g.get("icon")),
            "installed": installed,
        })
    mine.sort(key=lambda g: (not g["installed"], g["name"].lower()))
    invite_url = _invite_url()
    return render_template(
        "guild_picker.html",
        guilds=mine,
        invite_url=invite_url,
        bot_admin=is_bot_admin(),
        bot_name=os.getenv("BOT_NAME", "Gambly"),
    )


@app_routes.route("/dashboard/<gid>")
@guild_access_required
def dashboard_guild(gid):
    # Real bot membership, not just "does data exist for this ID" - a
    # server can have data from someone using a slash command there via
    # Discord's personal app-install without the bot ever joining it, and
    # a bot admin should still be able to look at leftover data for a
    # server like that even though it's not something they can "manage".
    if gid != d.DM_ID and gid not in dapi.bot_fetch_my_guilds() and not is_bot_admin():
        return render_template("errors/not_installed.html", invite_url=_invite_url()), 404
    return render_template("guild.html", gid=gid, bot_name=os.getenv("BOT_NAME", "Gambly"),
                            bot_admin=is_bot_admin())


@app_routes.route("/admin")
@bot_admin_required
def bot_admin_home():
    return render_template("bot_admin.html", bot_name=os.getenv("BOT_NAME", "Gambly"))


def _invite_url():
    client_id = os.getenv("DISCORD_CLIENT_ID", "")
    perms = os.getenv("DISCORD_BOT_PERMISSIONS", "1376805842006")
    guild_id = os.getenv("GUILD_ID", "").strip()
    params = {"client_id": client_id, "scope": "bot applications.commands", "permissions": perms}
    if guild_id:
        # Locks the install to this one server: Discord pre-selects it and
        # greys out the server picker so it can't be added anywhere else.
        params["guild_id"] = guild_id
        params["disable_guild_select"] = "true"
    return f"https://discord.com/oauth2/authorize?{urlencode(params)}"


# ------------------------------------------------------------------ /me ----
@app_routes.route("/api/me")
@login_required
def api_me():
    u = current_user()
    return jsonify({
        "id": u["id"],
        "username": u.get("global_name") or u["username"],
        "avatar_url": u["avatar_url"],
        "bot_admin": is_bot_admin(),
        "manageable_guilds": sorted(manageable_guild_ids()),
    })


# ----------------------------------------------------- single-guild data ----
@app_routes.route("/api/guild/<gid>")
@guild_access_required
def api_guild(gid):
    econ = d.load(d.econ_path_for(gid))
    cfg = d.load(d.cfg_file)
    g_econ = econ.get(gid, {"name": gid, "users": {}})
    is_personal = gid == d.DM_ID
    g_cfg = None if is_personal else d.guild_cfg(cfg, gid)

    users = []
    for uid, u in g_econ.get("users", {}).items():
        loan = u.get("loan")
        users.append({
            "id": uid,
            "name": u.get("name", uid),
            "bal": u.get("bal", 0),
            "bank": u.get("bank", 0),
            "loan_owed": loan["owed"] if loan else 0,
            "loan_defaulted": bool(loan and loan.get("defaulted")),
            "is_manager": False if is_personal else uid in [str(m) for m in g_cfg["managers"]],
            "is_banned": False if is_personal else (int(uid) in g_cfg["banned"] if uid.isdigit() else False),
            "daily_streak": u.get("daily_streak", 0),
            "win_streak": u.get("win_streak", 0),
            "business": (u.get("business") or {}).get("tier"),
            "badges": u.get("badges", []),
        })
    users.sort(key=lambda u: -(u["bal"] + u["bank"]))

    resp = {
        "id": gid,
        "name": d.guild_display_name(econ, cfg, gid),
        "icon": None,
        "is_personal": is_personal,
        "users": users,
    }
    if not is_personal:
        live = dapi.bot_fetch_guild(gid)
        if live:
            resp["icon"] = dapi.guild_icon_url(gid, live.get("icon"))
            # Prefer Discord's actual current name over whatever placeholder
            # got stamped into config.json before we'd ever seen this
            # server - fixes servers showing "unknown server" in the crumb
            # when they're installed but have no recorded activity yet.
            if live.get("name"):
                resp["name"] = live["name"]
        resp.update({
            "prefix": g_cfg.get("prefix", "!"),
            "manager_role": g_cfg.get("manager_role"),
            "jackpot": g_cfg.get("jackpot", 500),
            "tax_pct": g_cfg.get("tax_pct", 0),
            "server_pot": g_cfg.get("server_pot", 0),
            "testmode": g_cfg.get("testmode", False),
            "bounties": g_cfg.get("bounties", {}),
            "lottery_pot": g_cfg.get("lottery", {}).get("pot", 0),
            "staff_role_id": g_cfg.get("staff_role_id"),
            "ticket_category_id": g_cfg.get("ticket_category_id"),
            "ticket_ping_role_id": g_cfg.get("ticket_ping_role_id"),
            "game_channels": g_cfg.get("game_channels", {}),
            "custom_env": g_cfg.get("custom_env", {}),
            "game_list": sorted(cu.GAME_CHANNEL_ALIASES.keys()),
            "welcome_enabled": g_cfg.get("welcome_enabled", False),
            "welcome_channel_id": g_cfg.get("welcome_channel_id"),
            "welcome_message": g_cfg.get("welcome_message"),
            "leveling_enabled": g_cfg.get("leveling_enabled", True),
            "level_channel_id": g_cfg.get("level_channel_id"),
            "level_message": g_cfg.get("level_message"),
            "level_roles": g_cfg.get("level_roles", {}),
            "member_count_enabled": g_cfg.get("member_count_enabled", False),
            "member_count_channel_id": g_cfg.get("member_count_channel_id"),
            "member_count_template": g_cfg.get("member_count_template"),
            "logging": {
                log_type: {
                    "enabled": g_cfg.get(f"log_{log_type}_enabled", False),
                    "channel": g_cfg.get(f"log_{log_type}_channel"),
                }
                for log_type in ("message", "mod", "report", "withdraw", "deposit", "ticket")
            },
        })
    return jsonify(resp)


@app_routes.route("/api/guild/<gid>/roles")
@guild_access_required
def api_guild_roles(gid):
    if gid == d.DM_ID:
        return jsonify([])
    return jsonify(dapi.bot_fetch_roles(gid))


@app_routes.route("/api/guild/<gid>/channels")
@guild_access_required
def api_guild_channels(gid):
    if gid == d.DM_ID:
        return jsonify({"text": [], "voice": [], "categories": []})
    return jsonify(dapi.bot_fetch_channels(gid))


@app_routes.route("/api/guild/<gid>/leave", methods=["POST"])
@guild_access_required
def api_guild_leave(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not applicable"}), 400
    cfg = d.load(d.cfg_file)
    name = cfg.get(gid, {}).get("name", gid)
    ok = dapi.bot_leave_guild(gid)
    if not ok:
        return jsonify({"error": "Discord wouldn't let the bot leave - check the bot token."}), 502
    d.log_event(name, f"removed the bot from this server", actor=current_user()["username"])
    return jsonify({"ok": True})


# --------------------------------------------------- balance / chips ----
@app_routes.route("/api/guild/<gid>/balance", methods=["POST"])
@guild_access_required
def set_balance(gid):
    body = request.get_json() or {}
    uid = str(body.get("user_id"))
    try:
        new_bal = int(body["balance"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "balance must be a number"}), 400
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user yet - they need to run a command first"}), 400
    name = econ[gid]["users"][uid].get("name", uid)
    econ[gid]["users"][uid]["bal"] = max(0, new_bal)
    d.save(d.econ_path_for(gid), econ)
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"set {name}'s wallet to {econ[gid]['users'][uid]['bal']}", actor=current_user()["username"])
    return jsonify({"ok": True, "balance": econ[gid]["users"][uid]["bal"]})


@app_routes.route("/api/guild/<gid>/bank", methods=["POST"])
@guild_access_required
def set_bank(gid):
    body = request.get_json() or {}
    uid = str(body.get("user_id"))
    try:
        new_bank = int(body["bank"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "bank must be a number"}), 400
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user yet - they need to run a command first"}), 400
    name = econ[gid]["users"][uid].get("name", uid)
    econ[gid]["users"][uid]["bank"] = max(0, new_bank)
    d.save(d.econ_path_for(gid), econ)
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"set {name}'s bank to {econ[gid]['users'][uid]['bank']}", actor=current_user()["username"])
    return jsonify({"ok": True, "bank": econ[gid]["users"][uid]["bank"]})


@app_routes.route("/api/guild/<gid>/adjust", methods=["POST"])
@guild_access_required
def adjust_balance(gid):
    body = request.get_json() or {}
    uid = str(body.get("user_id"))
    try:
        amount = int(body["amount"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    reason = (body.get("reason") or "").strip()
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user yet - they need to run a command first"}), 400
    u = econ[gid]["users"][uid]
    u["bal"] = max(0, u.get("bal", 0) + amount)
    d.save(d.econ_path_for(gid), econ)
    verb = "gave" if amount >= 0 else "took"
    text = f"{verb} {abs(amount)} chips {'to' if amount >= 0 else 'from'} {u.get('name', uid)}, new balance {u['bal']}"
    if reason:
        text += f" (reason: {reason})"
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid), text, actor=current_user()["username"])
    return jsonify({"ok": True, "balance": u["bal"]})


@app_routes.route("/api/guild/<gid>/bulk_grant", methods=["POST"])
@guild_access_required
def bulk_grant(gid):
    body = request.get_json() or {}
    try:
        amount = int(body["amount"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or not econ[gid].get("users"):
        return jsonify({"error": "no players in that server yet"}), 400
    count = 0
    for u in econ[gid]["users"].values():
        u["bal"] = max(0, u.get("bal", 0) + amount)
        count += 1
    d.save(d.econ_path_for(gid), econ)
    verb = "granted" if amount >= 0 else "deducted"
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"{verb} {abs(amount)} chips {'to' if amount >= 0 else 'from'} all {count} players",
                actor=current_user()["username"])
    return jsonify({"ok": True, "affected": count})


# ------------------------------------------------------------------ loans ----
@app_routes.route("/api/guild/<gid>/loan/<uid>", methods=["POST"])
@guild_access_required
def edit_loan(gid, uid):
    body = request.get_json() or {}
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user"}), 400
    u = econ[gid]["users"][uid]
    if not u.get("loan"):
        return jsonify({"error": "this player doesn't have a loan"}), 400
    if "owed" in body:
        try:
            u["loan"]["owed"] = max(0, int(body["owed"]))
        except (TypeError, ValueError):
            return jsonify({"error": "owed must be a number"}), 400
    if "defaulted" in body:
        u["loan"]["defaulted"] = bool(body["defaulted"])
        u["loan"]["last_grow"] = time.time()
    d.save(d.econ_path_for(gid), econ)
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"edited {u.get('name', uid)}'s loan -> owed {u['loan']['owed']}, defaulted {u['loan']['defaulted']}",
                actor=current_user()["username"])
    return jsonify({"ok": True, "loan": u["loan"]})


@app_routes.route("/api/guild/<gid>/forgive/<uid>", methods=["POST"])
@guild_access_required
def forgive_loan(gid, uid):
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user"}), 400
    u = econ[gid]["users"][uid]
    if not u.get("loan"):
        return jsonify({"error": "this player doesn't owe anything"}), 400
    owed = u["loan"]["owed"]
    u["loan"] = None
    d.save(d.econ_path_for(gid), econ)
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"wiped {u.get('name', uid)}'s debt of {owed} clean", actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/reset/<uid>", methods=["POST"])
@guild_access_required
def reset_user(gid, uid):
    econ = d.load(d.econ_path_for(gid))
    if gid not in econ or uid not in econ[gid].get("users", {}):
        return jsonify({"error": "no record for that user"}), 400
    name = econ[gid]["users"][uid].get("name", uid)
    settings = d.load(d.settings_file)
    starting_bal = settings.get("starting_bal", d.SETTING_DEFAULTS["starting_bal"])
    econ[gid]["users"][uid] = d.new_acct(name, starting_bal)
    d.save(d.econ_path_for(gid), econ)
    d.log_event(d.guild_display_name(econ, d.load(d.cfg_file), gid),
                f"reset {name}'s account to a fresh {starting_bal}-chip start", actor=current_user()["username"])
    return jsonify({"ok": True})


# --------------------------------------------------- bans / managers ----
@app_routes.route("/api/guild/<gid>/ban/<uid>", methods=["POST"])
@guild_access_required
def toggle_ban(gid, uid):
    if gid == d.DM_ID:
        return jsonify({"error": "bans don't apply to the personal/DM economy"}), 400
    banned = bool((request.get_json() or {}).get("banned"))
    econ = d.load(d.econ_file)
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    uid_int = int(uid)
    name = econ.get(gid, {}).get("users", {}).get(uid, {}).get("name", uid)
    if banned and uid_int not in g["banned"]:
        g["banned"].append(uid_int)
    elif not banned and uid_int in g["banned"]:
        g["banned"].remove(uid_int)
    d.save(d.cfg_file, cfg)
    d.log_event(d.guild_display_name(econ, cfg, gid), f"{'banned' if banned else 'unbanned'} {name} from gambling",
                actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/manager/<uid>", methods=["POST"])
@guild_access_required
def toggle_manager(gid, uid):
    if gid == d.DM_ID:
        return jsonify({"error": "managers don't apply to the personal/DM economy"}), 400
    is_mgr = bool((request.get_json() or {}).get("manager"))
    econ = d.load(d.econ_file)
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    uid_int = int(uid)
    name = econ.get(gid, {}).get("users", {}).get(uid, {}).get("name", uid)
    if is_mgr and uid_int not in g["managers"]:
        g["managers"].append(uid_int)
    elif not is_mgr and uid_int in g["managers"]:
        g["managers"].remove(uid_int)
    d.save(d.cfg_file, cfg)
    d.log_event(d.guild_display_name(econ, cfg, gid),
                f"{'made' if is_mgr else 'removed'} {name} {'a manager' if is_mgr else 'as manager'}",
                actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/manager_role", methods=["POST"])
@guild_access_required
def set_manager_role(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    role_id = (request.get_json() or {}).get("role_id")
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    g["manager_role"] = int(role_id) if role_id else None
    d.save(d.cfg_file, cfg)
    d.log_event((g.get("name") or gid), f"set manager role id to {g['manager_role']}", actor=current_user()["username"])
    return jsonify({"ok": True, "manager_role": g["manager_role"]})


@app_routes.route("/api/guild/<gid>/prefix", methods=["POST"])
@guild_access_required
def set_prefix(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    prefix = ((request.get_json() or {}).get("prefix", "!"))[:3]
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    g["prefix"] = prefix
    d.save(d.cfg_file, cfg)
    d.log_event((g.get("name") or gid), f"set command prefix to '{prefix}'", actor=current_user()["username"])
    return jsonify({"ok": True, "prefix": prefix})


@app_routes.route("/api/guild/<gid>/config", methods=["POST"])
@guild_access_required
def set_guild_config(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    changed = []
    if "jackpot" in body:
        g["jackpot"] = max(0, int(body["jackpot"]))
        changed.append(f"jackpot -> {g['jackpot']}")
    if "tax_pct" in body:
        g["tax_pct"] = max(0, min(100, int(body["tax_pct"])))
        changed.append(f"tax_pct -> {g['tax_pct']}")
    if "testmode" in body:
        g["testmode"] = bool(body["testmode"])
        changed.append(f"testmode -> {g['testmode']}")
    if "server_pot" in body:
        g["server_pot"] = max(0, int(body["server_pot"]))
        changed.append(f"server_pot -> {g['server_pot']}")
    if "lottery_pot" in body:
        g["lottery"]["pot"] = max(0, int(body["lottery_pot"]))
        changed.append(f"lottery pot -> {g['lottery']['pot']}")
    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated server settings: {', '.join(changed)}", actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/moderation_config", methods=["POST"])
@guild_access_required
def set_moderation_config(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    changed = []

    def _opt_id(key):
        """None clears it, a digit string sets it, anything else is ignored.
        Stored as a STRING - see config_schema._env_id's docstring for why
        (JS number precision loss on big Discord snowflake IDs)."""
        if key not in body:
            return
        val = body[key]
        new_val = str(int(val)) if val not in (None, "") else None
        if g.get(key) != new_val:
            changed.append(f"{key} -> {new_val}")
        g[key] = new_val

    for key in ("staff_role_id", "ticket_category_id", "ticket_ping_role_id"):
        _opt_id(key)

    log_types = ("message", "mod", "report", "withdraw", "deposit", "ticket")
    for log_type in log_types:
        enabled_key, channel_key = f"log_{log_type}_enabled", f"log_{log_type}_channel"
        if enabled_key in body:
            g[enabled_key] = bool(body[enabled_key])
            changed.append(f"{enabled_key} -> {g[enabled_key]}")
        if channel_key in body:
            _opt_id(channel_key)

    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated moderation/logging settings: {', '.join(changed)}",
                    actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/game_channels", methods=["POST"])
@guild_access_required
def set_game_channels(gid):
    # {"game_channels": {"<game name>": "<channel id>" | null}} - null/""
    # clears the override and falls back to auto-detect-by-name again.
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    overrides = (request.get_json() or {}).get("game_channels", {})
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    gc = g.setdefault("game_channels", {})
    changed = []
    for game in cu.GAME_CHANNEL_ALIASES:
        if game not in overrides:
            continue
        val = overrides[game]
        if val in (None, ""):
            if gc.pop(game, None) is not None:
                changed.append(f"{game} -> auto-detect")
        else:
            try:
                new_val = str(int(val))
            except (TypeError, ValueError):
                continue
            if gc.get(game) != new_val:
                changed.append(f"{game} -> <#{new_val}>")
            gc[game] = new_val
    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated game channel locks: {', '.join(changed)}",
                    actor=current_user()["username"])
    return jsonify({"ok": True, "game_channels": gc})


@app_routes.route("/api/guild/<gid>/custom_env", methods=["POST"])
@guild_access_required
def set_custom_env(gid):
    # freeform KEY=VALUE lines, same shape as a real .env file - checked by
    # cog_utils.get_custom_setting() before the real environment variable
    # of the same name, per-server, no host/shell access needed.
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    raw = (request.get_json() or {}).get("raw", "")
    parsed = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key:
            parsed[key] = val.strip()
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    g["custom_env"] = parsed
    d.save(d.cfg_file, cfg)
    d.log_event((g.get("name") or gid), f"updated custom server env ({len(parsed)} key(s))",
                actor=current_user()["username"])
    return jsonify({"ok": True, "custom_env": parsed})


@app_routes.route("/api/guild/<gid>/welcome_config", methods=["POST"])
@guild_access_required
def set_welcome_config(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    changed = []
    if "welcome_enabled" in body:
        g["welcome_enabled"] = bool(body["welcome_enabled"])
        changed.append(f"welcome_enabled -> {g['welcome_enabled']}")
    if "welcome_channel_id" in body:
        val = body["welcome_channel_id"]
        g["welcome_channel_id"] = str(int(val)) if val not in (None, "") else None
        changed.append(f"welcome_channel_id -> {g['welcome_channel_id']}")
    if "welcome_message" in body:
        g["welcome_message"] = (body["welcome_message"] or "").strip() or None
        changed.append("welcome_message updated")
    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated welcome settings: {', '.join(changed)}",
                    actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/leveling_config", methods=["POST"])
@guild_access_required
def set_leveling_config(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    changed = []
    if "leveling_enabled" in body:
        g["leveling_enabled"] = bool(body["leveling_enabled"])
        changed.append(f"leveling_enabled -> {g['leveling_enabled']}")
    if "level_channel_id" in body:
        val = body["level_channel_id"]
        g["level_channel_id"] = str(int(val)) if val not in (None, "") else None
        changed.append(f"level_channel_id -> {g['level_channel_id']}")
    if "level_message" in body:
        g["level_message"] = (body["level_message"] or "").strip() or None
        changed.append("level_message updated")
    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated leveling settings: {', '.join(changed)}",
                    actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/level_roles", methods=["POST"])
@guild_access_required
def set_level_roles(gid):
    # {"level_roles": {"<level>": "<role id>"}} - full replace, since the
    # dashboard always sends its whole current table back (simplest to
    # reason about, and this table is never big enough for that to matter).
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    raw_roles = body.get("level_roles", {})
    parsed = {}
    for lvl, role_id in raw_roles.items():
        try:
            lvl_int = int(lvl)
            role_int = int(role_id)
        except (TypeError, ValueError):
            continue
        if lvl_int >= 0:
            parsed[str(lvl_int)] = str(role_int)  # string - see config_schema._env_id docstring
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    g["level_roles"] = parsed
    d.save(d.cfg_file, cfg)
    d.log_event((g.get("name") or gid), f"updated level roles ({len(parsed)} tier(s))",
                actor=current_user()["username"])
    return jsonify({"ok": True, "level_roles": parsed})


@app_routes.route("/api/guild/<gid>/member_count_config", methods=["POST"])
@guild_access_required
def set_member_count_config(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    changed = []
    if "member_count_enabled" in body:
        g["member_count_enabled"] = bool(body["member_count_enabled"])
        changed.append(f"member_count_enabled -> {g['member_count_enabled']}")
    if "member_count_channel_id" in body:
        val = body["member_count_channel_id"]
        g["member_count_channel_id"] = str(int(val)) if val not in (None, "") else None
        changed.append(f"member_count_channel_id -> {g['member_count_channel_id']}")
    if "member_count_template" in body:
        g["member_count_template"] = (body["member_count_template"] or "").strip() or None
        changed.append("member_count_template updated")
    d.save(d.cfg_file, cfg)
    if changed:
        d.log_event((g.get("name") or gid), f"updated member count settings: {', '.join(changed)}",
                    actor=current_user()["username"])
    return jsonify({"ok": True})


@app_routes.route("/api/guild/<gid>/level_leaderboard")
@guild_access_required
def api_level_leaderboard(gid):
    if gid == d.DM_ID:
        return jsonify([])
    levels = d.load(d.levels_file).get(gid, {})
    ranked = sorted(levels.items(), key=lambda kv: -kv[1].get("total_xp", 0))[:15]
    return jsonify([
        {"id": uid, "name": e.get("name", uid), "level": e.get("level", 0), "total_xp": e.get("total_xp", 0)}
        for uid, e in ranked
    ])


@app_routes.route("/api/guild/<gid>/bounty", methods=["POST"])
@guild_access_required
def set_bounty(gid):
    if gid == d.DM_ID:
        return jsonify({"error": "not available for the personal/DM economy"}), 400
    body = request.get_json() or {}
    uid = str(body.get("user_id", ""))
    amount = int(body.get("amount", 0) or 0)
    cfg = d.load(d.cfg_file)
    g = d.guild_cfg(cfg, gid)
    econ = d.load(d.econ_file)
    name = econ.get(gid, {}).get("users", {}).get(uid, {}).get("name", uid)
    if amount <= 0:
        g["bounties"].pop(uid, None)
        d.log_event((g.get("name") or gid), f"cleared the bounty on {name}", actor=current_user()["username"])
    else:
        g["bounties"][uid] = amount
        d.log_event((g.get("name") or gid), f"set a {amount}-chip bounty on {name}", actor=current_user()["username"])
    d.save(d.cfg_file, cfg)
    return jsonify({"ok": True, "bounties": g["bounties"]})


@app_routes.route("/api/guild/<gid>/activity")
@guild_access_required
def api_guild_activity(gid):
    cfg = d.load(d.cfg_file)
    econ = d.load(d.econ_path_for(gid))
    name = d.guild_display_name(econ, cfg, gid)
    entries = d.load(d.log_file) if isinstance(d.load(d.log_file), list) else []
    entries = [e for e in entries if e.get("guild") == name]
    q = (request.args.get("q") or "").strip().lower()
    if q:
        entries = [e for e in entries if q in e.get("text", "").lower()]
    return jsonify(list(reversed(entries))[:200])


@app_routes.route("/api/guild/<gid>/export.csv")
@guild_access_required
def export_csv(gid):
    econ = d.load(d.econ_path_for(gid))
    g_econ = econ.get(gid, {"users": {}})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "wallet", "bank", "total", "loan_owed", "loan_defaulted"])
    for uid, u in g_econ.get("users", {}).items():
        loan = u.get("loan")
        w.writerow([uid, u.get("name", ""), u.get("bal", 0), u.get("bank", 0),
                    u.get("bal", 0) + u.get("bank", 0),
                    loan["owed"] if loan else 0, bool(loan and loan.get("defaulted"))])
    fname = f"balances_{gid}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": f"attachment; filename={fname}"})


# ============================================================================
# Bot-admin-only surface: cross-server search, global defaults, DM ledger,
# CSV export of every server. Only reachable if BOT_ADMIN_IDS is set AND the
# logged-in Discord ID is in it - see auth.py. Nothing here is on by default.
# ============================================================================
@app_routes.route("/api/overview")
@bot_admin_required
def api_overview():
    econ = d.load(d.econ_file)
    personal = d.load(d.personal_econ_file)
    cfg = d.load(d.cfg_file)
    bot_guilds = dapi.bot_fetch_my_guilds()
    data_ids = set(econ.keys()) | set(cfg.keys())

    total_players = total_wallet = total_bank = total_debt = defaulted_count = 0
    banned_ids = set()
    for gid, g in cfg.items():
        for uid in g.get("banned", []):
            banned_ids.add((gid, str(uid)))

    for gid, g in list(econ.items()) + [(d.DM_ID, personal.get(d.DM_ID, {}))]:
        for uid, u in g.get("users", {}).items():
            total_players += 1
            total_wallet += u.get("bal", 0)
            total_bank += u.get("bank", 0)
            loan = u.get("loan")
            if loan:
                total_debt += loan.get("owed", 0)
                if loan.get("defaulted"):
                    defaulted_count += 1

    return jsonify({
        "guild_count": len(data_ids | set(bot_guilds.keys())),
        "installed_count": len(bot_guilds),
        # has activity/config data but the bot isn't actually a member -
        # almost always means someone used a slash command there through
        # a personal app install, not a real "add to server"
        "ghost_count": len(data_ids - set(bot_guilds.keys())),
        "total_players": total_players,
        "total_wallet": total_wallet,
        "total_bank": total_bank,
        "total_chips_in_circulation": total_wallet + total_bank,
        "total_debt_owed": total_debt,
        "defaulted_count": defaulted_count,
        "banned_count": len(banned_ids),
    })


@app_routes.route("/api/all_guilds")
@bot_admin_required
def api_all_guilds():
    econ = d.load(d.econ_file)
    cfg = d.load(d.cfg_file)
    personal = d.load(d.personal_econ_file)
    bot_guilds = dapi.bot_fetch_my_guilds()

    out = []
    for gid in set(bot_guilds.keys()) | set(econ.keys()) | set(cfg.keys()):
        installed = gid in bot_guilds
        live = bot_guilds.get(gid)
        name = live["name"] if live else d.guild_display_name(econ, cfg, gid)
        # Nothing anywhere knows this server's real name (not installed, no
        # config/economy record with a name in it) - all we've got is the
        # raw snowflake ID, which is an ugly thing to show as a title.
        unnamed = not live and name == gid
        out.append({
            "id": gid,
            "name": "Unknown server" if unnamed else name,
            "unnamed": unnamed,
            "icon": live["icon"] if live else None,
            "installed": installed,
            "user_count": len(econ.get(gid, {}).get("users", {})),
            "banned_count": len(cfg.get(gid, {}).get("banned", [])),
        })
    if d.DM_ID in personal:
        out.append({
            "id": d.DM_ID, "name": d.guild_display_name(econ, cfg, d.DM_ID), "icon": None,
            "installed": True,
            "user_count": len(personal.get(d.DM_ID, {}).get("users", {})), "banned_count": 0,
        })
    # installed servers first (matches the "Your servers" picker), then alphabetical
    out.sort(key=lambda g: (not g["installed"], g["name"].lower()))
    return jsonify(out)


@app_routes.route("/api/settings")
@bot_admin_required
def api_get_settings():
    return jsonify({**d.SETTING_DEFAULTS, **d.load(d.settings_file)})


@app_routes.route("/api/settings", methods=["POST"])
@bot_admin_required
def api_set_settings():
    body = request.get_json() or {}
    current = d.load(d.settings_file)
    changed = []
    for key in d.SETTING_DEFAULTS:
        if key in body:
            try:
                val = float(body[key])
                if key in ("starting_bal", "daily_amt", "loan_max"):
                    val = int(val)
            except (TypeError, ValueError):
                continue
            if current.get(key) != val:
                changed.append(f"{key} -> {val}")
            current[key] = val
    d.save(d.settings_file, current)
    if changed:
        d.log_event("global", f"updated bot settings: {', '.join(changed)}", actor=current_user()["username"])
    return jsonify({**d.SETTING_DEFAULTS, **current})


@app_routes.route("/api/search")
@bot_admin_required
def api_search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify([])
    econ = d.load(d.econ_file)
    personal = d.load(d.personal_econ_file)
    cfg = d.load(d.cfg_file)
    results = []
    for gid, g in list(econ.items()) + [(d.DM_ID, personal.get(d.DM_ID, {}))]:
        gname = d.guild_display_name(econ, cfg, gid)
        for uid, u in g.get("users", {}).items():
            name = u.get("name", uid)
            if q in uid.lower() or q in str(name).lower():
                results.append({"guild_id": gid, "guild_name": gname, "user_id": uid,
                                 "name": name, "bal": u.get("bal", 0), "bank": u.get("bank", 0)})
    results.sort(key=lambda r: -(r["bal"] + r["bank"]))
    return jsonify(results[:100])


@app_routes.route("/api/export_all.csv")
@bot_admin_required
def export_all_csv():
    econ = d.load(d.econ_file)
    personal = d.load(d.personal_econ_file)
    cfg = d.load(d.cfg_file)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["guild_id", "guild_name", "user_id", "name", "wallet", "bank", "total", "loan_owed", "loan_defaulted"])
    for gid, g in list(econ.items()) + [(d.DM_ID, personal.get(d.DM_ID, {}))]:
        gname = d.guild_display_name(econ, cfg, gid)
        for uid, u in g.get("users", {}).items():
            loan = u.get("loan")
            w.writerow([gid, gname, uid, u.get("name", ""), u.get("bal", 0), u.get("bank", 0),
                        u.get("bal", 0) + u.get("bank", 0),
                        loan["owed"] if loan else 0, bool(loan and loan.get("defaulted"))])
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=balances_all_servers.csv"})


# ------------------------------------------------------------- app factory ----
def create_app():
    app = Flask(__name__)
    app.secret_key = os.getenv("FLASK_SECRET_KEY")
    if not app.secret_key:
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
            "and put it in your .env file."
        )
    app.permanent_session_lifetime = timedelta(days=7)
    # Server-side sessions: a logged-in person's guild list from Discord can
    # be too big to fit in a browser cookie (Flask's default), especially
    # for someone in a lot of servers. Filesystem storage needs zero setup
    # for a single-process deploy; point SESSION_TYPE at redis (and set
    # SESSION_REDIS) if you're running multiple dashboard instances behind
    # a load balancer.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    )
    if _ServerSession:
        session_dir = Path(__file__).parent.parent / ".flask_session"
        session_dir.mkdir(exist_ok=True)
        app.config.update(
            SESSION_TYPE=os.getenv("SESSION_TYPE", "filesystem"),
            SESSION_FILE_DIR=str(session_dir),
            SESSION_PERMANENT=True,
        )
        _ServerSession(app)
    else:
        print("Flask-Session isn't installed (pip install -r requirements.txt) - "
              "falling back to cookie-based sessions, which can break for accounts "
              "in a lot of Discord servers.")

    app.register_blueprint(auth_bp)
    app.register_blueprint(app_routes)

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    return app