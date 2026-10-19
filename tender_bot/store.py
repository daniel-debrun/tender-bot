"""State: one SQLite file. ``seen`` (every accession processed once), ``offers`` (one row per original SC TO-I,
with a state and a JSON blob), ``events`` (append-only audit trail per offer, also used for the P&L ledger)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
create table if not exists seen (acc text primary key, form text, cik integer, filed text, accepted text,
                                 seen_at text, result text);
create table if not exists offers (id text primary key, cik integer, name text, state text, data text,
                                   created_at text, updated_at text);
create table if not exists events (n integer primary key autoincrement, offer_id text, at text, kind text,
                                   data text);
create index if not exists events_offer on events(offer_id);
"""

#: offer states. Terminal: INELIGIBLE, SKIPPED, CLOSED. DRYRUN can still be entered with --place.
OPEN_STATES = ("HELD", "TENDERED", "SELL_REQUESTED", "EXIT_PENDING", "AWAIT_PAYMENT")
WAITING_STATES = ("WATCH", "DRYRUN")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self):
        self.db.execute("begin immediate")
        try:
            yield
        except BaseException:
            self.db.execute("rollback")
            raise
        self.db.execute("commit")

    # --- seen --------------------------------------------------------------------------------------------
    def is_seen(self, acc: str) -> bool:
        return self.db.execute("select 1 from seen where acc=?", (acc,)).fetchone() is not None

    def mark_seen(self, f: dict, result: str) -> None:
        self.db.execute("insert or replace into seen values (?,?,?,?,?,?,?)",
                        (f["acc"], f.get("form"), f.get("cik"), f.get("filed"), f.get("accepted"), utcnow(), result))

    # --- offers ------------------------------------------------------------------------------------------
    def add_offer(self, oid: str, cik: int, name: str, state: str, data: dict) -> None:
        now = utcnow()
        self.db.execute("insert or ignore into offers values (?,?,?,?,?,?,?)",
                        (oid, cik, name, state, json.dumps(data, default=str), now, now))

    def get(self, oid: str) -> dict | None:
        r = self.db.execute("select id, cik, name, state, data, created_at, updated_at from offers where id=?",
                            (oid,)).fetchone()
        return _row(r) if r else None

    def find(self, ref: str) -> list[dict]:
        """Offers whose id starts with ``ref`` or whose ticker equals it (CLI lookups)."""
        ref = ref.strip()
        return [o for o in self.offers() if o["id"].startswith(ref) or (o.get("ticker") or "").upper() == ref.upper()]

    def offers(self, states: tuple | None = None) -> list[dict]:
        q = "select id, cik, name, state, data, created_at, updated_at from offers"
        if states:
            q += f" where state in ({','.join('?' * len(states))})"
        return [_row(r) for r in self.db.execute(q + " order by created_at", states or ())]

    def update(self, oid: str, state: str | None = None, **fields) -> dict:
        o = self.get(oid)
        if o is None:
            raise KeyError(oid)
        data = {k: v for k, v in o.items() if k not in ("id", "cik", "name", "state", "created_at", "updated_at")}
        data.update(fields)
        self.db.execute("update offers set state=?, data=?, updated_at=? where id=?",
                        (state or o["state"], json.dumps(data, default=str), utcnow(), oid))
        return self.get(oid)

    def by_cik(self, cik: int) -> list[dict]:
        return [o for o in self.offers() if o["cik"] == int(cik)]

    # --- events ------------------------------------------------------------------------------------------
    def event(self, oid: str, kind: str, **data) -> None:
        self.db.execute("insert into events (offer_id, at, kind, data) values (?,?,?,?)",
                        (oid, utcnow(), kind, json.dumps(data, default=str)))

    def events(self, oid: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "select offer_id, at, kind, data from events where 1=1", []
        if oid:
            q, args = q + " and offer_id=?", args + [oid]
        if kind:
            q, args = q + " and kind=?", args + [kind]
        return [dict(offer_id=a, at=b, kind=c, **json.loads(d)) for a, b, c, d in self.db.execute(q + " order by n", args)]


def _row(r) -> dict:
    oid, cik, name, state, data, created, updated = r
    return dict(json.loads(data), id=oid, cik=cik, name=name, state=state, created_at=created, updated_at=updated)
