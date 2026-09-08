import os
import secrets
import time

from flask import Blueprint, redirect, request, session, url_for, render_template, abort
from functools import wraps

from . import discord_api as dapi

bp = Blueprint("auth", __name__)

# Optional, off by default: a small allowlist of Discord user IDs (set as
# a comma-separated env var, never hardcoded here) that get a "bot admin"
# section of the dashboard - cross-server search, global economy defaults,
# and the personal/DM ledger. Everyone else only ever sees servers where
# Discord itself already says they can manage the server. Leave the env
# var unset and this feature simply doesn't exist for anyone.
BOT_ADMIN_IDS = {i.strip() for i in os.getenv("BOT_ADMIN_IDS", "").split(",") if i.strip()}


def current_user():
    return session.get("user")


def is_bot_admin():
    u = current_user()
    return bool(u) and u["id"] in BOT_ADMIN_IDS


def _refresh_if_needed():
    tok = session.get("token")
    if not tok:
        return False
    expires_at = tok["obtained_at"] + tok.get("expires_in", 0) - 60
    if time.time() < expires_at:
        return True
    try:
        new_tok = dapi.refresh_token(tok["refresh_token"])
        session["token"] = new_tok
        return True
    except Exception:
        return False


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user() or not _refresh_if_needed():
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


def bot_admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not current_user() or not _refresh_if_needed():
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        if not is_bot_admin():
            abort(403)
        return view(*a, **kw)
    return wrapped


def manageable_guild_ids():
    """Guild IDs (strings) the logged-in person is allowed to administer,
    per the permissions Discord itself reports for their account."""
    guilds = session.get("guilds") or []
    return {g["id"] for g in guilds if dapi.can_manage(g)}


def guild_access_required(view):
    """Gate an /api/guild/<gid>/... or /dashboard/<gid> route: the caller
    must own or manage that specific server (or be a bot admin)."""
    @wraps(view)
    def wrapped(gid, *a, **kw):
        if not current_user() or not _refresh_if_needed():
            session.clear()
            return redirect(url_for("auth.login", next=request.path))
        if not is_bot_admin() and gid not in manageable_guild_ids():
            abort(403)
        return view(gid, *a, **kw)
    return wrapped


@bp.route("/login")
def login():
    if not dapi.CLIENT_ID or not dapi.CLIENT_SECRET:
        return render_template("errors/setup_needed.html"), 500
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    session["next"] = request.args.get("next") or url_for("app_routes.dashboard_home")
    return redirect(dapi.authorize_url(state))


@bp.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return render_template("errors/login_failed.html", reason=error), 400

    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        return render_template("errors/login_failed.html", reason="state_mismatch"), 400

    code = request.args.get("code")
    if not code:
        return render_template("errors/login_failed.html", reason="no_code"), 400

    try:
        token = dapi.exchange_code(code)
        user = dapi.fetch_user(token["access_token"])
        guilds = dapi.fetch_user_guilds(token["access_token"])
    except Exception:
        return render_template("errors/login_failed.html", reason="discord_unreachable"), 502

    session.clear()
    session["token"] = token
    session["user"] = {
        "id": user["id"],
        "username": user.get("username", "unknown"),
        "global_name": user.get("global_name"),
        "avatar_url": dapi.avatar_url(user["id"], user.get("avatar"), user.get("discriminator", "0")),
    }
    session["guilds"] = guilds
    session.permanent = True

    dest = session.pop("next", None) or url_for("app_routes.dashboard_home")
    return redirect(dest)


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("app_routes.landing"))
