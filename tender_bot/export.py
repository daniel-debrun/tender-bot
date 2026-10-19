"""``tender_bot export``: the bot's record as flat JSON lists.

* ``offers.json``: one row per SC TO-I candidate the bot has seen (eligible or not), as it stands now.
* ``outcomes.json``: one row per CLOSED offer (the P&L ledger).
* ``fills.json``: every entry / sell / paper-cleanup fill in the event log, for reconciliation with the broker.
* ``events.json``: a point-in-time table: one row per state change, each holding only what was
  known at its ``published`` time (UTC). Kinds: ``seen`` (the bot first processed the filing), ``skip`` (an offer
  that was eligible at detection and later skipped or found untradable), ``entry`` (the entry fill), ``close``.

Dates are ISO; times in ``offers``/``outcomes`` are US Eastern (``decided_at``, ``closed_at``), ``published`` and
fill ``at`` are UTC. Every string is scrubbed of IB account ids.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from ibkr_ops.config import redact
from ibkr_ops.gates import PRO_FIXED

from . import offer, timing
from .bot import pnl

FILES = ("offers.json", "outcomes.json", "fills.json", "events.json")
ACCOUNT_RE = re.compile(r"\b(?:D[UF][A-Z]?|U|F|I)\d{5,9}\b")       # ibkr-ops's account pattern
FILL_KINDS = {"sell_fill": "sell", "paper_cleanup_fill": "paper_cleanup"}
EVENT_COLUMNS = ("event_id", "published", "kind", "filed", "entry_date", "ticker", "cik", "state_at_publish",
                 "eligible", "skip_reason", "dutch", "cap_price", "oddlot_priority", "any_all", "expire",
                 "entry_price", "qty", "spy_entry", "close_how", "closed_date", "days", "cost", "proceeds", "ret",
                 "spy_ret", "mkt_adj", "simulated_tender", "estimated")
KIND_ORDER = {"seen": 0, "skip": 1, "entry": 2, "close": 3}


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)      # store timestamps without a zone are UTC


def _et(ts: str | None) -> str | None:
    d = _dt(ts)
    return d.astimezone(timing.ET).isoformat(timespec="seconds") if d else None


def _et_date(ts: str | None) -> str | None:
    d = _dt(ts)
    return d.astimezone(timing.ET).date().isoformat() if d else None


def _utc(ts: str | None) -> str | None:
    d = _dt(ts)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else None


def _ticker(o: dict) -> str | None:
    """The SEC ticker known at detection (the IB symbol for offers without one)."""
    t = [t for t, _ in o.get("tickers") or []]
    return t[0] if t else o.get("symbol")


def _original_cap(o: dict) -> float | None:
    """The cap as filed (before any amendment): the top of the Dutch range or the fixed price."""
    return o.get("hi") if o.get("dutch") else o.get("fixed")


def _bool(v):
    return None if v is None else bool(v)


def _scrub(rows: list[dict]) -> list[dict]:
    return [{k: ACCOUNT_RE.sub(lambda m: redact(m.group(0)), v) if isinstance(v, str) else v for k, v in r.items()}
            for r in rows]


def _skip_reason(o: dict) -> str | None:
    return "; ".join(o.get("reasons") or []) or None if o["state"] in ("SKIPPED", "INELIGIBLE") else None


def _decided_at(o: dict, events: list[dict]) -> str | None:
    if o.get("decided_at"):
        return _et(o["decided_at"])
    if o.get("entry_at"):
        return _et(o["entry_at"])
    for kind in ("skipped", "resolve_failed", "entry_order"):           # records from before decided_at existed
        ev = [e for e in events if e["kind"] == kind]
        if ev:
            return _et(ev[-1]["at"])
    return _et(o.get("detected_at") or o.get("created_at")) if o["state"] == "INELIGIBLE" else None


def _pnl(o: dict, events: list[dict]) -> dict:
    return o.get("pnl") or pnl(o, events)


# --- the four tables -----------------------------------------------------------------------------------------
def offer_rows(st) -> list[dict]:
    rows = []
    for o in st.offers():
        ev = st.events(o["id"])
        rows.append(dict(
            id=o["id"], cik=o["cik"], ticker=_ticker(o), name=o["name"], filed=o.get("filed"),
            accepted=o.get("accepted"), state=o["state"], skip_reason=_skip_reason(o), dutch=_bool(o.get("dutch")),
            cap_price=offer.cap_price(o), oddlot_priority=_bool(o.get("oddlot_priority")),
            any_all=_bool(o.get("any_all")), expire=o.get("expire"), expire_final=o.get("expire_final"),
            entry_date=_et_date(o.get("entry_at")), entry_price=o.get("entry_price"), qty_held=o.get("qty_held"),
            spy_entry=o.get("spy_entry"), decided_at=_decided_at(o, ev), outcome=o.get("outcome")))
    return rows


def outcome_rows(st) -> list[dict]:
    rows = []
    for o in st.offers(("CLOSED",)):
        p = _pnl(o, st.events(o["id"]))
        rows.append(dict(id=o["id"], entry_date=_et_date(o.get("entry_at")), closed_at=_et(o.get("closed_at")),
                         close_how=o.get("close_how"), days=p.get("days"), cost=p.get("cost"),
                         proceeds=p.get("proceeds"), ret=p.get("ret"), spy_ret=p.get("spy_ret"),
                         mkt_adj=p.get("mkt_adj"), simulated_tender=bool(p.get("simulated_tender")),
                         estimated=bool(p.get("estimated"))))
    return rows


def fill_rows(st) -> list[dict]:
    rows = []
    for e in st.events():
        if e["kind"] == "entry_order" and float((e.get("result") or {}).get("filled") or 0) > 0:
            r = e["result"]
            qty, px = float(r["filled"]), float(r.get("avg_price") or r["limit"])
            rows.append(dict(id=e["offer_id"], kind="entry", at=_utc(e["at"]), qty=qty, price=px,
                             commission_est=PRO_FIXED.estimate(qty, px)))
        elif e["kind"] in FILL_KINDS:
            rows.append(dict(id=e["offer_id"], kind=FILL_KINDS[e["kind"]], at=_utc(e["at"]), qty=float(e["qty"]),
                             price=float(e["price"]), commission_est=e.get("commission_est")))
    return rows


def event_rows(st) -> list[dict]:
    rows = []
    for o in st.offers():
        ev = st.events(o["id"])
        det = next((e for e in ev if e["kind"] == "detected"), None)
        seen_state = det["state"] if det else o["state"] if o["state"] == "INELIGIBLE" else "WATCH"
        seen_reasons = det.get("reasons") if det else (o.get("reasons") if seen_state == "INELIGIBLE" else None)
        static = dict(event_id=o["id"], filed=o.get("filed"), ticker=_ticker(o), cik=o["cik"],
                      dutch=_bool(o.get("dutch")), cap_price=_original_cap(o),
                      oddlot_priority=_bool(o.get("oddlot_priority")), any_all=_bool(o.get("any_all")),
                      expire=o.get("expire"))

        def row(kind, published, **known):
            r = dict.fromkeys(EVENT_COLUMNS)
            r.update(static, kind=kind, published=_utc(published), **known)
            return r

        rows.append(row("seen", o.get("detected_at") or o.get("created_at"), state_at_publish=seen_state,
                        eligible=seen_state == "WATCH", skip_reason="; ".join(seen_reasons or []) or None))
        if seen_state == "WATCH" and o["state"] in ("SKIPPED", "INELIGIBLE"):
            rows.append(row("skip", o.get("decided_at") or o.get("updated_at"), state_at_publish=o["state"],
                            eligible=False, skip_reason=_skip_reason(o)))
        if o.get("entry_at"):
            entry = dict(entry_date=_et_date(o["entry_at"]), entry_price=o.get("entry_price"), qty=o.get("qty_held"),
                         spy_entry=o.get("spy_entry"))
            rows.append(row("entry", o["entry_at"], state_at_publish="HELD", eligible=True, **entry))
            if o["state"] == "CLOSED" and o.get("closed_at"):
                p = _pnl(o, ev)
                rows.append(row(
                    "close", o["closed_at"], state_at_publish="CLOSED", eligible=True, **entry,
                    cap_price=offer.cap_price(o), expire=o.get("expire_final") or o.get("expire"),
                    close_how=o.get("close_how"), closed_date=_et_date(o["closed_at"]), days=p.get("days"),
                    cost=p.get("cost"), proceeds=p.get("proceeds"), ret=p.get("ret"), spy_ret=p.get("spy_ret"),
                    mkt_adj=p.get("mkt_adj"), simulated_tender=bool(p.get("simulated_tender")),
                    estimated=bool(p.get("estimated"))))
    rows.sort(key=lambda r: (r["published"] or "", KIND_ORDER[r["kind"]], r["event_id"]))
    return rows


def write(st, out: str | os.PathLike) -> dict[str, int]:
    """Write the four files into ``out``; returns {file: rows}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tables = {"offers.json": offer_rows(st), "outcomes.json": outcome_rows(st), "fills.json": fill_rows(st),
              "events.json": event_rows(st)}
    for name, rows in tables.items():
        tmp = out / f".{name}.tmp"
        tmp.write_text(json.dumps(_scrub(rows), indent=1, default=str) + "\n")
        tmp.replace(out / name)
    return {k: len(v) for k, v in tables.items()}
