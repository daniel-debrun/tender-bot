"""From a filing to an offer record, and the eligibility rules (pure functions, no I/O)."""
from __future__ import annotations

import re
from datetime import date, datetime

from . import parse, textutil, timing

PARSED_FIELDS = ("title", "cusip", "sclass", "exchange", "dutch", "lo", "hi", "fixed", "sought", "sought_usd",
                 "oddlot", "oddlot_priority", "any_all", "oddlot_only", "expire")
REQUIRED = ("sclass", "expire")   # plus a price: fixed, or lo/hi for a Dutch auction


def read(path) -> tuple[dict, list[dict]]:
    raw = textutil.load(path)
    h = textutil.header(raw)
    m = re.search(r"<ACCEPTANCE-DATETIME>(\d{14})", raw[:2000])     # textutil.header expects a colon here
    h["accepted"] = m.group(1) if m else h.get("accepted")
    return h, textutil.docs(raw)


def from_original(header: dict, docs: list[dict]) -> dict:
    """Parsed fields of an SC TO-I original, with ``sources`` recording where each field came from."""
    r = parse.parse_original(docs)
    out = {k: r.get(k) for k in PARSED_FIELDS}
    if isinstance(out["expire"], date):
        out["expire"] = out["expire"].isoformat()
    out["sources"] = {k: "regex" for k, v in out.items() if v is not None}
    out["header"] = {k: header.get(k) for k in ("form", "filed", "accepted", "subj_cik", "subj_name", "sic")}
    return out


def cap_price(o: dict) -> float | None:
    """The most the offer can pay per share: the fixed price or the top of the Dutch range (amended if so)."""
    if o.get("dutch"):
        return o.get("hi_final") or o.get("hi")
    return o.get("price_change") or o.get("fixed")


def missing(o: dict) -> list[str]:
    m = [k for k in REQUIRED if o.get(k) in (None, "", "unknown")]
    if cap_price(o) is None:
        m.append("price")
    return m


def edgar_eligibility(o: dict, cfg: dict, today: date) -> list[str]:
    """Reasons an offer is out of scope using filing data only (empty list = eligible so far)."""
    r = []
    if o.get("sclass") != "common":
        r.append(f"security class {o.get('sclass')!r}, not common stock")
    if o.get("exchange"):
        r.append("exchange offer, not cash")
    if cap_price(o) is None:
        r.append("no offer price or range parsed")
    if o.get("oddlot_only") and cfg["exclude_oddlot_only"]:
        r.append("odd-lot-only program (eligibility usually fixed at a past record date)")
    if cfg["require_oddlot_provision"] and not o.get("oddlot_priority"):
        if not (cfg["allow_any_and_all"] and o.get("any_all")):
            r.append("no odd-lot priority provision")
    exp = o.get("expire")
    if not exp:
        r.append("no expiration date parsed")
    else:
        ed = date.fromisoformat(o["entry_day"]) if o.get("entry_day") else today
        days = timing.trading_days_between(ed, date.fromisoformat(exp))
        if days < cfg["min_business_days_to_expiry"]:
            r.append(f"expires {exp}, {days} trading days after entry (min {cfg['min_business_days_to_expiry']})")
    return r


def contract_eligibility(c: dict | None, cfg: dict) -> list[str]:
    """Reasons from the IBKR contract (stock type, listing)."""
    if not c:
        return ["no IBKR contract resolved"]
    r = []
    if c.get("stock_type") not in cfg["allowed_stock_types"]:
        r.append(f"IBKR stock type {c.get('stock_type')!r} not in {cfg['allowed_stock_types']}")
    if (c.get("primary_exchange") or "").upper() not in {x.upper() for x in cfg["allowed_exchanges"]}:
        r.append(f"primary exchange {c.get('primary_exchange')!r} not allowed")
    if (c.get("currency") or "USD") != "USD":
        r.append(f"currency {c.get('currency')}")
    return r


def price_guard(ask: float | None, o: dict, cfg: dict) -> str | None:
    """Skip when the ask is at or above the cap."""
    cap = cap_price(o)
    if ask is None:
        return "no ask quote"
    if cap is None:
        return "no offer price"
    if ask >= cap * (1 - cfg["min_edge"]):
        return f"ask {ask:.2f} >= cap {cap:.2f}" + (f" x (1 - {cfg['min_edge']:g})" if cfg["min_edge"] else "")
    return None


def shares_to_buy(held: float, pending_buys: float, external: int, cfg: dict) -> tuple[int, str | None]:
    """The 99-share guard: total holdings across every account must stay below 100. Buy ``cfg['shares']`` only
    when nothing is held or pending anywhere; existing holdings mean a skip (no top-ups)."""
    total = float(held) + float(pending_buys) + int(external)
    want = int(cfg["shares"])
    if total != 0:
        return 0, f"already hold or have pending {total:g} shares (positions {held:g}, open buys {pending_buys:g}, " \
                  f"external {external})"
    if want >= 100:
        return 0, "configured shares >= 100 would lose odd-lot priority"
    return want, None


def entry_status(o: dict, today: date, cfg: dict) -> str:
    """``due`` (today is the entry day or within the catch-up allowance), ``early`` or ``late``."""
    ed = date.fromisoformat(o["entry_day"])
    if today < ed:
        return "early"
    if timing.trading_days_between(ed, today) > cfg["entry_catchup_days"]:
        return "late"
    return "due"


def parse_accepted(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timing.ET)   # SEC header ACCEPTANCE-DATETIME
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timing.ET)
