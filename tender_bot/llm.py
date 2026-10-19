"""Optional LLM fallback (config llm_fallback) for fields the regex parser leaves empty. It loads an external
``llm_extract.py`` from config llm_script_dir (not included). Only missing fields are filled; each filled field is
recorded with source "haiku" so it can be told apart from the regex output."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

log = logging.getLogger("tender_bot")

SYSTEM = ("You read SEC Schedule TO-I issuer tender offer documents and extract facts exactly as stated. "
          "Use null when the document does not state a value. Prices are per share in US dollars.")
SCHEMA = {
    "type": "object",
    "properties": {
        "security_class": {"type": "string", "enum": ["common", "preferred", "debt", "warrants", "options", "other"]},
        "exchange_offer": {"type": "boolean"},
        "dutch_auction": {"type": "boolean"},
        "price_low": {"type": ["number", "null"]},
        "price_high": {"type": ["number", "null"]},
        "fixed_price": {"type": ["number", "null"]},
        "odd_lot_priority": {"type": "boolean"},
        "expiration_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
    },
    "required": ["security_class", "exchange_offer", "dutch_auction", "price_low", "price_high", "fixed_price",
                 "odd_lot_priority", "expiration_date"],
}
MAP = {"sclass": "security_class", "expire": "expiration_date"}


def make(cfg: dict):
    """A callable ``(offer, docs, missing) -> offer`` or None when the helper script isn't available."""
    path = Path(cfg["llm_script_dir"]).expanduser() / "llm_extract.py"
    if not path.exists():
        log.warning("llm_fallback: %s not found; disabled", path)
        return None
    spec = importlib.util.spec_from_file_location("llm_extract", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cache = str(Path(cfg["state_dir"]) / "llm_cache.jsonl")

    def fill(o: dict, docs: list[dict], missing: list[str]) -> dict:
        text = "\n\n".join(d["text"] for d in docs)[:60000]
        try:
            res = mod.run_tasks([(o["acc"], text)], SCHEMA, SYSTEM, cache, workers=1)
        except Exception as e:  # noqa: BLE001 - the regex result stands
            log.warning("llm fallback failed for %s: %s", o["acc"], e)
            return o
        r = res.get(o["acc"])
        if not r:
            return o
        o = dict(o, sources=dict(o.get("sources") or {}))
        if "sclass" in missing and r.get("security_class"):
            o["sclass"], o["sources"]["sclass"] = r["security_class"], "haiku"
        if "expire" in missing and r.get("expiration_date"):
            o["expire"] = o["expire_final"] = r["expiration_date"]
            o["sources"]["expire"] = "haiku"
        if "price" in missing:
            if r.get("dutch_auction") and r.get("price_low") and r.get("price_high"):
                o.update(dutch=True, lo=r["price_low"], hi=r["price_high"], lo_final=r["price_low"], hi_final=r["price_high"])
                o["sources"].update(lo="haiku", hi="haiku", dutch="haiku")
            elif r.get("fixed_price"):
                o.update(dutch=False, fixed=r["fixed_price"])
                o["sources"].update(fixed="haiku", dutch="haiku")
        return o

    return fill
