"""tender_bot command line. Scheduled commands run under ibkr-ops's job guard (lock, UTC logs, schedule window,
heartbeat, exit codes, alert); user commands act on the local state only and never place orders.

    tender_bot scan [--evening]              EDGAR only (IBKR contract lookup if a Gateway is up, read-only)
    tender_bot trade [--entries] [--place [--live]]   exits (sells); --entries also buys today's offers
    tender_bot deadline                      tender reminders and the pre-deadline price check
    tender_bot summary                       daily summary (file + notification)
    tender_bot status | show ID | ledger     inspect
    tender_bot export [--out DIR]            offers/outcomes/fills/events .json (default: the current folder)
    tender_bot confirm ID | sell ID          after tendering in IBKR / to sell instead of tendering
    tender_bot kill | unkill                 create / remove the kill-switch file (blocks every order)
    tender_bot heartbeats [--alert]          ibkr-ops heartbeat check of the tender jobs (exit 1 on a problem)
    tender_bot parse ACCESSION CIK           download and parse one filing (debug)
    tender_bot smoke [--symbol F] [--place]  paper order-path test: buy 1 share then sell it (market hours)

Without --place every order is a dry run. --place sends to a paper account only; a live account additionally
needs config ibkr.allow_live=true, --live, and IBKR_ALLOW_LIVE=1 (checked by ibkr-ops's gates).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from ibkr_ops import cron, probe
from ibkr_ops.config import Endpoint
from ibkr_ops.session import Session

from . import amend, config, edgar, export, llm, notify, offer, store, timing
from .bot import Bot, describe, format_pnl, pnl, tender_instructions

log = logging.getLogger("tender_bot")

#: (job name, UTC window centre, tolerance minutes): systemd fires these at fixed US Eastern times, so the UTC
#: window spans both EDT and EST. --manual skips the window (not the trading-day check).
#: the scheduled jobs' heartbeats and the most hours allowed since each one's last run (ibkr-ops heartbeats)
HEARTBEATS = ("tender-scan=2", "tender-deadline=96", "tender-trade-am=96", "tender-trade-entries=96",
              "tender-summary=96")
WINDOWS = {"trade-am": ("14:30Z", 35), "trade-entries": ("20:20Z", 35), "deadline": ("14:10Z", 35),
           "summary": ("22:00Z", 35)}


def _paths(cfg):
    sd = Path(cfg["state_dir"])
    return sd, sd / "tender.db", sd / "orders.jsonl", sd / "KILL"


def _make(cfg, now=None):
    sd, db, _, _ = _paths(cfg)
    st = store.Store(db)
    ed = edgar.Edgar(cfg["user_agent"], sd / "edgar", cfg["sec_min_gap_s"])
    nt = notify.Notifier(cfg)
    fill = llm.make(cfg) if cfg["llm_fallback"] else None
    return Bot(cfg, st, ed, nt, now=now, llm=fill)


def _endpoint(cfg) -> Endpoint:
    return Endpoint.resolve({"host": cfg["ibkr"]["host"], "port": cfg["ibkr"]["port"]})


def _broker(bot, session, place=False, live=False):
    from .broker import Broker

    _, _, ledger, kill = _paths(bot.cfg)
    return Broker(session, bot.cfg, place=place, allow_live=live, ledger=str(ledger), kill_switch=str(kill))


# --- scheduled jobs ----------------------------------------------------------------------------------------
def job_scan(args, cfg) -> int:
    def main(job):
        bot = _make(cfg)
        def scan():
            r = bot.scan(evening=args.evening)
            log.info("scan: %s", r)
            if r["failed"]:     # each is retried on the next scan; the run still exits nonzero (PARTIAL)
                raise RuntimeError(f"{r['failed']} of {r['new']} new filings failed")
        job.step("edgar", scan)
        ep = _endpoint(cfg)
        if probe.port_open(ep.host, ep.port):
            def res():
                with Session(ep, role="cron-quotes") as s:
                    log.info("resolved %d offers", bot.resolve(_broker(bot, s)))
            job.step("resolve", res)
        return cron.OK
    return cron.run("tender-scan", main, require_gateway=False)


def job_trade(args, cfg) -> int:
    live = bool(args.live and cfg["ibkr"]["allow_live"])
    if args.live and not cfg["ibkr"]["allow_live"]:
        print("--live given but config ibkr.allow_live is false: refusing", file=sys.stderr)
        return cron.REFUSED

    def main(job):
        bot = _make(cfg)
        with Session(_endpoint(cfg), role="cron-orders", readonly=not args.place) as s:
            b = _broker(bot, s, place=args.place, live=live)
            job.step("resolve", bot.resolve, b)
            job.step("exits", lambda: log.info("exits: %s", bot.exits(b)))
            if args.entries:
                job.step("entries", lambda: log.info("entries: %s", bot.entries(b, placing=args.place)))
        return cron.OK

    name = "trade-entries" if args.entries else "trade-am"
    return cron.run(f"tender-{name}", main, endpoint=_endpoint(cfg), trading_day=True,
                    window=None if args.manual else WINDOWS[name])


def job_deadline(args, cfg) -> int:
    def main(job):
        bot = _make(cfg)
        ep = _endpoint(cfg)
        if probe.port_open(ep.host, ep.port):
            with Session(ep, role="cron-quotes") as s:
                job.step("deadlines", lambda: log.info("deadlines: %s", bot.deadlines(_broker(bot, s))))
        else:
            log.warning("gateway down: reminders without quotes")
            job.step("deadlines", lambda: log.info("deadlines: %s", bot.deadlines(None)))
        return cron.OK
    return cron.run("tender-deadline", main, require_gateway=False, trading_day=True,
                    window=None if args.manual else WINDOWS["deadline"])


def summary_text(bot) -> str:
    st = bot.st
    lines = [f"tender-bot summary {timing.now_et():%Y-%m-%d %H:%M} ET"]
    open_ = st.offers(store.OPEN_STATES)
    lines.append(f"open positions: {len(open_)}")
    for o in open_:
        exp = o.get("expire_final") or o.get("expire")
        lines.append(f"  [{o['state']}] {o['id']} {describe(o)}; held {o.get('qty_held'):g} @ ${o.get('entry_price', 0):.2f}; "
                     f"IBKR deadline {timing.ibkr_deadline(date.fromisoformat(exp)):%Y-%m-%d %H:%M} ET")
    waiting = st.offers(store.WAITING_STATES)
    lines.append(f"waiting for entry: {len(waiting)}")
    lines += [f"  [{o['state']}] {o['id']} {describe(o)}; entry {o.get('entry_day')}" for o in waiting]
    day = timing.now_et().date().isoformat()
    recent = [o for o in st.offers(("INELIGIBLE", "SKIPPED")) if (o.get("updated_at") or "")[:10] >= day]
    lines.append(f"ineligible/skipped today: {len(recent)}")
    lines += [f"  [{o['state']}] {o['name']}: {'; '.join(o.get('reasons') or [])}" for o in recent]
    closed = st.offers(("CLOSED",))
    if closed:
        ps = [o.get("pnl") or pnl(o, st.events(o["id"])) for o in closed]
        rets = [p["ret"] for p in ps if p.get("ret") is not None]
        tot = sum(p["pnl"] for p in ps)
        lines.append(f"closed: {len(closed)}, total P&L ${tot:,.2f}, mean return "
                     f"{(sum(rets) / len(rets) if rets else 0):+.2%}")
    return "\n".join(lines)


def job_summary(args, cfg) -> int:
    def main(job):
        bot = _make(cfg)
        text = summary_text(bot)
        out = Path(cfg["state_dir"]) / "summary" / f"{timing.now_et():%Y-%m-%d}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
        first = text.splitlines()
        bot.nt.send("daily summary", "\n".join(first[1:])[:1500], "low")
        print(text)
        return cron.OK
    return cron.run("tender-summary", main, require_gateway=False,
                    window=None if args.manual else WINDOWS["summary"])


# --- user commands -----------------------------------------------------------------------------------------
def _one(bot, ref):
    hits = bot.st.find(ref)
    if len(hits) != 1:
        raise SystemExit(f"{len(hits)} offers match {ref!r}: use more of the accession number")
    return hits[0]


def cmd(args, cfg) -> int:
    bot = _make(cfg)
    sd, _, _, kill = _paths(cfg)
    if args.cmd == "status":
        print(summary_text(bot))
    elif args.cmd == "show":
        o = _one(bot, args.id)
        print(json.dumps({k: v for k, v in o.items() if k != "header"}, indent=1, default=str))
        for e in bot.st.events(o["id"]):
            print(e["at"], e["kind"], json.dumps({k: v for k, v in e.items() if k not in ("at", "kind", "offer_id")}, default=str)[:300])
        if o["state"] == "HELD":
            print("\n" + tender_instructions(o))
    elif args.cmd == "confirm":
        o = bot.confirm(_one(bot, args.id)["id"])
        print(f"{o['id']} {o.get('symbol')}: TENDERED; reminders stop")
    elif args.cmd == "sell":
        o = bot.request_sell(_one(bot, args.id)["id"])
        print(f"{o['id']} {o.get('symbol')}: SELL_REQUESTED; the next trade run sells (with --place)")
    elif args.cmd == "ledger":
        for o in bot.st.offers(("CLOSED",)):
            print(o["id"], o.get("symbol"), format_pnl(o.get("pnl") or pnl(o, bot.st.events(o["id"]))).splitlines()[0])
    elif args.cmd == "export":
        out = Path(args.out or Path.cwd())
        n = export.write(bot.st, out)
        print(f"wrote to {out}: " + ", ".join(f"{k} {v} rows" for k, v in n.items()))
    elif args.cmd == "kill":
        kill.parent.mkdir(parents=True, exist_ok=True)
        kill.write_text(f"created {store.utcnow()}\n")
        print(f"kill switch on: {kill} (every order is refused until it is removed)")
    elif args.cmd == "unkill":
        kill.unlink(missing_ok=True)
        print("kill switch off")
    elif args.cmd == "parse":
        p = bot.ed.full_text_path(int(args.cik), args.acc)
        h, d = offer.read(p)
        o = offer.from_original(h, d) if h.get("form") == "SC TO-I" else amend.read_amendment(d)
        print(json.dumps(o, indent=1, default=str))
    return 0


def smoke(args, cfg) -> int:
    """Buy 1 share and sell it back through the same gates the bot uses. Paper only: live is never allowed here."""
    from ib_async import Stock

    logging.basicConfig(level=logging.INFO)
    bot = _make(cfg)
    with Session(_endpoint(cfg), role="interactive", readonly=not args.place) as s:
        b = _broker(bot, s, place=args.place, live=False)
        d = s.qualify(Stock(args.symbol, "SMART", "USD"))
        if d is None:
            print(f"no contract for {args.symbol}")
            return 1
        stamp = timing.now_et().strftime("%Y%m%dT%H%M%S")
        q = b.quote(d.conId)
        print("quote", {k: q.get(k) for k in ("bid", "ask", "price_type", "at")})
        r = b.order(d.conId, 1, q, idem=f"smoke:{args.symbol}:buy:{stamp}")
        print("buy ", r)
        if r.get("filled"):
            q = b.quote(d.conId)
            r2 = b.order(d.conId, -int(r["filled"]), q, idem=f"smoke:{args.symbol}:sell:{stamp}")
            print("sell", r2)
            if not r2.get("filled"):
                print(f"WARNING: the buy filled but the sell did not: sell {r['filled']:g} {args.symbol} in the paper account")
                return 4
        return 0 if r["mode"] in ("dry-run", "paper") and not r["blocked"] else 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tender_bot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--evening", action="store_true")
    p = sub.add_parser("trade")
    p.add_argument("--entries", action="store_true")
    p.add_argument("--place", action="store_true", help="send orders (paper unless --live)")
    p.add_argument("--live", action="store_true", help="permit a live account (also needs config + IBKR_ALLOW_LIVE=1)")
    p.add_argument("--manual", action="store_true", help="run outside the scheduled window")
    for c in ("deadline", "summary"):
        sub.add_parser(c).add_argument("--manual", action="store_true")
    for c in ("status", "ledger", "kill", "unkill"):
        sub.add_parser(c)
    for c in ("show", "confirm", "sell"):
        sub.add_parser(c).add_argument("id")
    sub.add_parser("export").add_argument("--out", help="folder (default: the current folder)")
    sub.add_parser("heartbeats").add_argument("--alert", action="store_true", help="alert via $IBKR_OPS_ALERT_CMD")
    p = sub.add_parser("smoke")
    p.add_argument("--symbol", default="F")
    p.add_argument("--place", action="store_true")
    p = sub.add_parser("parse")
    p.add_argument("acc")
    p.add_argument("cik")
    args = ap.parse_args(argv)
    cfg = config.load(args.config)
    if args.cmd in ("scan", "trade", "deadline", "summary"):
        return {"scan": job_scan, "trade": job_trade, "deadline": job_deadline, "summary": job_summary}[args.cmd](args, cfg)
    if args.cmd == "smoke":
        return smoke(args, cfg)
    if args.cmd == "heartbeats":
        from ibkr_ops import cli as ibkr_cli

        return ibkr_cli.main(["heartbeats", *HEARTBEATS] + (["--alert"] if args.alert else []))
    logging.basicConfig(level=logging.WARNING)
    return cmd(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
