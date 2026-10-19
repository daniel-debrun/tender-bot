"""Apply an SC TO-I/A amendment to a tracked offer."""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import parse


_EXT = re.compile(r"\b(?:has|have|had)\s+(?:been\s+)?extended|\b(?:is|are|was|were)\s+(?:hereby\s+|being\s+)?extend(?:ed|ing)"
                  r"|\bby\s+extending|\bannounc\w+\s+(?:an?\s+|the\s+)?extension|\b(?:decided|elected)\s+to\s+extend"
                  r"|\b(?:it|we|company)\s+(?:has|had|have)?\s*extend(?:ed|ing)\s+the\s+(?:expiration|offer|tender)", re.I)
_COND = re.compile(r"\b(?:unless|if|may|would|might|could|whether|in\s+the\s+event|can|should|such\s+later)\b", re.I)


def extension(text: str) -> tuple[str | None, str | None]:
    """(new expiration date, evidence) from an affirmative extension statement, else (None, None).

    Deliberately strict: conditional text such as "unless the Offer is extended" does not count. A statement counts when no conditional word appears in the 80 characters before the
    verb; the date is the latest one within 400 characters after it, before any ", unless" clause."""
    t = re.sub(r"\s+", " ", text)
    for m in _EXT.finditer(t):
        if _COND.search(t[max(0, m.start() - 80):m.start()]):
            continue
        tail = re.split(r",?\s+unless\b", t[m.start():m.end() + 400], maxsplit=1, flags=re.I)[0]
        if not re.search(r"expir", t[max(0, m.start() - 200):m.end() + 400], re.I):
            continue
        dates = [d for d in (parse.pdate(x.group(1)) for x in re.finditer(parse.DATE, tail, re.I)) if d]
        if dates:
            return max(dates).isoformat(), t[max(0, m.start() - 120):m.end() + 200]
    return None, None


def read_amendment(docs: list[dict]) -> dict:
    r = parse.parse_amend(docs)
    r = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()}
    r["extension"], r["extension_evidence"] = extension("\n".join(d["text"] for d in docs)[:600_000])
    return r


def apply(o: dict, a: dict, filed: str) -> tuple[dict, list[str]]:
    """(field updates for the offer, human-readable changes). Rules:

    * extension: an affirmative extension sentence (``extension``) giving a date within 60
      days of the amendment and later than the current expiration;
    * price change (fixed) / new range (Dutch, only when hi > lo);
    * termination: a termination sentence, unless results were already reported;
    * completion: results with a purchase sentence (``results_strict``), or results reported on or after the
      expiration date (a forward-looking "will announce results" before expiry is not a completion).
    """
    upd, notes = {}, []
    fd = date.fromisoformat(filed)
    exp = o.get("expire_final") or o.get("expire")
    if a.get("extension"):
        ne = date.fromisoformat(a["extension"])
        if ne <= fd + timedelta(days=60) and (exp is None or ne > date.fromisoformat(exp)):
            upd["expire_final"] = ne.isoformat()
            notes.append(f"expiration extended to {ne} (\"{(a.get('extension_evidence') or '')[:160]}\")")
    if a.get("price_change") and not o.get("dutch"):
        upd["price_change"] = a["price_change"]
        notes.append(f"price changed to ${a['price_change']:.2f}")
    if a.get("new_lo") and a.get("new_hi") and a["new_hi"] > a["new_lo"] and o.get("dutch"):
        upd["lo_final"], upd["hi_final"] = a["new_lo"], a["new_hi"]
        notes.append(f"range changed to ${a['new_lo']:.2f}-${a['new_hi']:.2f}")
    expired = exp is not None and fd >= date.fromisoformat(upd.get("expire_final", exp))
    if a.get("terminated") and o.get("outcome") != "completed":
        upd["outcome"], upd["term_date"] = "terminated", filed
        notes.append("offer TERMINATED")
    elif a.get("results_strict") or (a.get("results") and expired):
        upd["outcome"], upd["res_date"] = "completed", o.get("res_date") or filed
        for k in ("final_price", "purchased", "tendered", "proration", "undersub", "oversub"):
            if a.get(k) not in (None, False):
                upd[k] = a[k]
        fp = a.get("final_price")
        notes.append("results reported" + (f": final price ${fp:.2f}" if fp else "")
                     + (f", proration {a['proration']:.1%}" if a.get("proration") else ""))
    return upd, notes
