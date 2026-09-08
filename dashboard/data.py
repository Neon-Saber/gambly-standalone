import json
import time
from pathlib import Path

import config_schema as cfgschema

BASE = Path(__file__).parent.parent
econ_file = BASE / "economy.json"
personal_econ_file = BASE / "personal_economy.json"
cfg_file = cfgschema.CFG_FILE
log_file = BASE / "activity.json"
settings_file = BASE / "settings.json"

DM_ID = "dm"

SETTING_DEFAULTS = {
    "starting_bal": 1000,
    "daily_amt": 250,
    "bank_interest": 0.02,
    "loan_max": 2000,
    "loan_interest": 0.20,
}


def load(path):
    if not path.exists():
        return {}
    with open(path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)


def econ_path_for(gid):
    return personal_econ_file if gid == DM_ID else econ_file


def log_event(guild_name, text, actor=None):
    entries = []
    if log_file.exists():
        with open(log_file, "r") as f:
            try:
                entries = json.load(f)
            except json.JSONDecodeError:
                entries = []
    tag = f"[dashboard: {actor}]" if actor else "[dashboard]"
    entries.append({"ts": time.time(), "guild": guild_name, "text": f"{tag} {text}"})
    entries = entries[-500:]
    save(log_file, entries)


def guild_cfg(cfg, gid):
    return cfgschema.ensure_guild(cfg, gid)


def guild_display_name(econ, cfg, gid):
    if gid == DM_ID:
        return "Direct Messages (personal)"
    return econ.get(gid, {}).get("name") or cfg.get(gid, {}).get("name") or gid


def known_guild_ids():
    """Every guild ID the bot has ever touched (has config and/or an
    economy bucket for) - i.e. servers the bot is actually installed in,
    as far as the flat files can tell us."""
    econ = load(econ_file)
    cfg = load(cfg_file)
    return set(econ.keys()) | set(cfg.keys())


def new_acct(name, starting_bal=None):
    return {
        "name": name, "bal": starting_bal if starting_bal is not None else SETTING_DEFAULTS["starting_bal"],
        "bank": 0,
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
