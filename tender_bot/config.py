"""Configuration: JSON file merged over DEFAULTS. Unknown keys are rejected so a typo can't silently relax a rule."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "state_dir": "~/.local/state/tender-bot",
    # EDGAR: declared User-Agent and a minimum gap between requests (0.5 s = 2 req/s, well under SEC's 10/s)
    "user_agent": "",                    # yours: "Name email" (config user_agent or $TENDER_BOT_USER_AGENT)
    "sec_min_gap_s": 0.5,
    "backfill_days": 3,                   # daily-index days re-checked by the evening scan
    # eligibility
    "require_oddlot_provision": True,     # the offer must give odd-lot holders priority (or be any-and-all)
    "allow_any_and_all": True,            # any-and-all offers have no proration, so the odd-lot clause is moot
    "exclude_oddlot_only": True,          # odd-lot-only programs usually need shares held at a past record date
    "allowed_stock_types": ["COMMON"],    # IBKR stockType: common stock only (no CEFs, REITs, ADRs)
    "allowed_exchanges": ["NYSE", "NASDAQ", "AMEX", "ARCA", "BATS", "IEX"],   # IBKR primaryExchange (no OTC)
    "min_edge": 0.0,                      # buy only if ask < cap * (1 - min_edge); cap = fixed price or top of range
    "min_business_days_to_expiry": 3,     # entry day -> expiration; T+1 settlement + IBKR's noon-before deadline
    # sizing and caps
    "shares": 99,
    "max_trade_value": 10000.0,
    "max_open_positions": 5,
    "max_total_value": 30000.0,
    "entry_catchup_days": 1,              # trading days after the planned entry day on which a missed entry may run
    "slippage": 0.005,                    # marketable limit: ask * (1 + slippage), bid * (1 - slippage)
    "rest_seconds": 90,                   # unfilled quantity is cancelled after this
    "external_holdings": {},              # {"TICKER": shares} held anywhere this Gateway login can't see
    # tender reminders and exits
    "reminder_business_days": 2,          # remind daily from this many trading days before IBKR's deadline
    "auto_sell_on_termination": True,
    "auto_sell_remainder": True,          # unaccepted shares returned after a completed offer
    "auto_sell_above_offer": False,       # sell instead of tendering if the bid is above the cap before expiry
    "payment_lag_days": 3,                # trading days after the results filing before payment is expected
    # optional Haiku fallback for fields the regex parser misses (scripts/llm_extract.py, Claude CLI)
    "llm_fallback": False,
    "llm_script_dir": "",                 # folder holding llm_extract.py (not included)
    # notifications: desktop notify-send plus a local log; "command" (or env IBKR_OPS_ALERT_CMD) gets the message
    "notify": {"desktop": True, "command": None},
    # IBKR (ibkr-ops); live trading needs allow_live here AND --live AND IBKR_ALLOW_LIVE=1
    "ibkr": {"host": "127.0.0.1", "port": 4002, "allow_live": False, "allow_delayed_quotes": False,
             "max_quote_age_s": 120, "max_daily_value": 30000.0},
}


class ConfigError(ValueError):
    pass


def _merge(base: dict, over: dict, path: str = "") -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k not in base:
            raise ConfigError(f"unknown config key {path}{k}")
        if isinstance(base[k], dict) and k != "external_holdings":
            if not isinstance(v, dict):
                raise ConfigError(f"{path}{k} must be an object")
            out[k] = _merge(base[k], v, f"{path}{k}.")
        else:
            out[k] = v
    return out


def _source(path) -> Path | None:
    env = os.environ
    if path or env.get("TENDER_BOT_CONFIG"):
        return Path(path or env["TENDER_BOT_CONFIG"]).expanduser()
    return Path.home() / ".config/tender-bot/config.json"


def load(path: str | os.PathLike | None = None) -> dict:
    """Config from ``path``, else $TENDER_BOT_CONFIG, else ~/.config/tender-bot/config.json, else defaults.
    $TENDER_BOT_STATE overrides state_dir and $TENDER_BOT_USER_AGENT the SEC User-Agent."""
    p = _source(path)
    over = json.loads(p.read_text()) if p is not None and p.exists() else {}
    cfg = _merge(DEFAULTS, over)
    cfg["state_dir"] = str(Path(os.environ.get("TENDER_BOT_STATE") or cfg["state_dir"]).expanduser())
    cfg["user_agent"] = os.environ.get("TENDER_BOT_USER_AGENT") or cfg["user_agent"]
    cfg["external_holdings"] = {str(k).upper(): int(v) for k, v in cfg["external_holdings"].items()}
    if not 0 < int(cfg["shares"]) < 100:
        raise ConfigError("shares must be 1..99: odd-lot priority covers holders of fewer than 100 shares")
    return cfg
