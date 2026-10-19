"""The bot's lifecycle: detect -> parse -> check -> enter -> tender (by the user) -> exit -> ledger.

Offer states (store.py): WATCH (eligible, waiting for its entry close) · DRYRUN (a dry run "entered") ·
INELIGIBLE · SKIPPED (eligible but not entered: price guard, caps, holdings, too late) · HELD (bought, tender not
confirmed) · TENDERED (user confirmed the election) · SELL_REQUESTED (sell instead of tendering) · EXIT_PENDING
(offer terminated: sell) · AWAIT_PAYMENT (results reported) · CLOSED.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from ibkr_ops.gates import PRO_FIXED

from . import amend, offer, timing
from .store import OPEN_STATES, WAITING_STATES

log = logging.getLogger("tender_bot")

LISTED = {"NYSE", "NASDAQ", "CBOE", "NYSE ARCA", "NYSE MKT", "NYSE AMERICAN", "BATS"}
LIVE_STATES = WAITING_STATES + OPEN_STATES
CLOSE_SIMULATED = "paid (paper tender simulated)"


def filing_url(cik: int, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{acc}-index.htm"


def describe(o: dict) -> str:
    if o.get("dutch"):
        px = f"Dutch auction ${o.get('lo_final') or o.get('lo'):.2f}-${offer.cap_price(o):.2f}"
    elif offer.cap_price(o) is not None:
        px = f"fixed ${offer.cap_price(o):.2f}"
    else:
        px = "price not parsed"
    odd = "odd-lot priority" if o.get("oddlot_priority") else "any-and-all" if o.get("any_all") else "NO odd-lot clause"
    tick = o.get("symbol") or ",".join(t for t, _ in o.get("tickers") or []) or "?"
    return f"{o['name']} ({tick}): {px}, {odd}, expires {o.get('expire_final') or o.get('expire')}"


def tender_instructions(o: dict) -> str:
    dl = timing.ibkr_deadline(date.fromisoformat(o.get("expire_final") or o["expire"]))
    lines = [
        f"Tender all {int(o.get('qty_held') or 0)} shares of {o.get('symbol')} in IBKR Client Portal:",
        "  Help > Support Center > Corporate Actions Manager (Voluntary CA Election) > this offer,",
        f"  elect to TENDER exactly {int(o.get('qty_held') or 0)} shares (IBKR needs an exact number, not 'all')",
    ]
    if o.get("dutch"):
        lines.append("  Dutch auction: choose 'at the purchase price determined by the offer' (price taker)")
    lines += [
        "  tick the odd-lot / fewer-than-100-shares certification if the form shows one",
        f"  IBKR deadline: {dl:%a %Y-%m-%d %H:%M} ET (noon the trading day before expiration)",
        "  if the offer is not in the tool: Secure Message Center > New Ticket > Account Services >",
        "  Corporate Actions/Dividends, naming the security, symbol and exact quantity",
        f"Then run: tender_bot confirm {o['id']}",
        f"Filing: {filing_url(o['cik'], o['id'])}",
    ]
    return "\n".join(lines)


class Bot:
    def __init__(self, cfg: dict, store, edgar, notifier, now: datetime | None = None, llm=None):
        self.cfg, self.st, self.ed, self.nt = cfg, store, edgar, notifier
        self._now = now
        self.llm = llm

    @property
    def now(self) -> datetime:
        return (self._now or timing.now_et()).astimezone(timing.ET)

    @property
    def today(self) -> date:
        return self.now.date()

    # --- detection --------------------------------------------------------------------------------------
    def scan(self, evening: bool = False) -> dict:
        """Process new SC TO-I(/A) filings from the latest-filings feed; in the evening also the daily indexes of
        the last ``backfill_days`` trading days and each tracked issuer's submissions (catches anything missed)."""
        filings = self.ed.latest()
        if evening or not self.st.db.execute("select 1 from seen limit 1").fetchone():
            d = self.today
            for _ in range(self.cfg["backfill_days"]):
                filings += self._source(self.ed.daily, d)
                d = timing.prev_trading_day(d)
        if evening:
            for o in self.st.offers(LIVE_STATES):
                filings += [f for f in self._source(self.ed.submissions, o["cik"]) if f["form"] == "SC TO-I/A"
                            and f["filed"] >= o["filed"]]
        new, done = {}, 0
        for f in filings:
            if not self.st.is_seen(f["acc"]) and f["acc"] not in new:
                new[f["acc"]] = f
        for f in sorted(new.values(), key=lambda f: (f.get("filed") or "", f.get("accepted") or "")):
            try:
                self.process(f)
                done += 1
            except Exception as e:  # noqa: BLE001 - one bad filing must not block the rest; retried next scan
                log.exception("filing %s failed: %s", f["acc"], e)
        return {"feed": len(filings), "new": len(new), "processed": done, "failed": len(new) - done}

    @staticmethod
    def _source(fn, arg) -> list[dict]:
        """A backfill source that fails is logged and skipped; the latest-filings feed stays authoritative."""
        try:
            return fn(arg)
        except Exception as e:  # noqa: BLE001
            log.warning("%s(%s) failed: %s", fn.__name__, arg, e)
            return []

    def process(self, f: dict) -> None:
        if f["form"] == "SC TO-I":
            self.new_offer(f)
        elif f["form"] == "SC TO-I/A":
            self.amendment(f)
        else:
            self.st.mark_seen(f, "other form")

    def new_offer(self, f: dict) -> None:
        cik, acc = int(f["cik"]), f["acc"]
        listed = [(t, ex) for t, ex in self.ed.tickers().get(cik, []) if ex.upper() in LISTED]
        base = dict(acc=acc, filed=f["filed"], accepted=f.get("accepted"), tickers=listed, url=filing_url(cik, acc),
                    detected_at=self.now.isoformat())
        if not listed:          # interval funds, BDCs, private and OTC issuers: not downloaded
            self.st.add_offer(acc, cik, f.get("name", ""), "INELIGIBLE", dict(base, reasons=["no exchange-listed ticker"],
                                                                              decided_at=self.now.isoformat()))
            self.st.mark_seen(f, "ineligible: unlisted")
            return
        header, docs = offer.read(self.ed.full_text_path(cik, acc))
        if header.get("form") != "SC TO-I":
            self.st.mark_seen(f, f"form {header.get('form')}")
            return
        o = dict(base, **offer.from_original(header, docs))
        accepted = offer.parse_accepted(f.get("accepted")) or offer.parse_accepted(header.get("accepted"))
        filed = date.fromisoformat(f["filed"]) if f.get("filed") else self.today
        o.update(accepted=accepted.isoformat() if accepted else None,
                 entry_day=timing.entry_day(accepted, filed).isoformat(),
                 expire_final=o.get("expire"), lo_final=o.get("lo"), hi_final=o.get("hi"), name=f.get("name") or header.get("subj_name"))
        gaps = offer.missing(o)
        if gaps and self.llm is not None:
            o = self.llm(o, docs, gaps)
        reasons = offer.edgar_eligibility(o, self.cfg, self.today)
        state = "INELIGIBLE" if reasons else "WATCH"
        o["reasons"] = reasons
        if reasons:
            o["decided_at"] = self.now.isoformat()
        self.st.add_offer(acc, cik, o["name"] or "", state, {k: v for k, v in o.items() if k != "name"})
        self.st.event(acc, "detected", state=state, reasons=reasons, sources=o.get("sources"))
        if state == "WATCH":
            self.nt.send(f"new tender offer: {o['name']}",
                         describe(dict(o, id=acc)) + f"\nEntry: buy {self.cfg['shares']} shares at the "
                         f"close on {o['entry_day']}.\n{o['url']}")
        self.st.mark_seen(f, state)

    def amendment(self, f: dict) -> None:
        cik = int(f["cik"])
        cands = [o for o in self.st.by_cik(cik) if o["state"] in LIVE_STATES and o["filed"] <= (f.get("filed") or "9")
                 and (not f.get("filed") or date.fromisoformat(f["filed"]) - date.fromisoformat(o["filed"]) <= timedelta(days=400))]
        if not cands:
            self.st.mark_seen(f, "untracked amendment")
            return
        o = max(cands, key=lambda o: o["filed"])
        _, docs = offer.read(self.ed.full_text_path(cik, f["acc"]))
        a = amend.read_amendment(docs)
        upd, notes = amend.apply(o, a, f.get("filed") or self.today.isoformat())
        state = o["state"]
        if upd.get("outcome") == "terminated":
            if state in WAITING_STATES:
                state, upd["reasons"] = "SKIPPED", ["terminated before entry"]
                upd["decided_at"] = self.now.isoformat()
            elif state in OPEN_STATES and state != "EXIT_PENDING":
                state = "EXIT_PENDING" if self.cfg["auto_sell_on_termination"] else state
        elif upd.get("outcome") == "completed" and state in ("HELD", "TENDERED"):
            state = "AWAIT_PAYMENT"
            upd["payment_expected"] = timing.add_trading_days(
                date.fromisoformat(upd["res_date"]), self.cfg["payment_lag_days"]).isoformat()
            upd["tender_confirmed_before_results"] = o["state"] == "TENDERED"
        o = self.st.update(o["id"], state, **upd)
        self.st.event(o["id"], "amendment", acc=f["acc"], notes=notes, parsed=a)
        if notes and o["state"] not in ("INELIGIBLE",):
            urgent = "terminated" in " ".join(notes).lower() and state in OPEN_STATES
            extra = ""
            if state == "EXIT_PENDING":
                extra = "\nThe bot will sell the shares at the next trade run (auto_sell_on_termination)."
            elif upd.get("outcome") == "terminated" and state in OPEN_STATES:
                extra = f"\nauto_sell_on_termination is off: sell manually or run tender_bot sell {o['id']}."
            elif state == "AWAIT_PAYMENT" and not upd.get("tender_confirmed_before_results"):
                extra = "\nYou never confirmed the tender: if you did not tender, the shares are still held."
            self.nt.send(f"{o['name']}: " + "; ".join(notes), describe(o) + extra + f"\n{filing_url(cik, f['acc'])}",
                         "critical" if urgent else "normal")
        self.st.mark_seen(f, "amendment: " + ("; ".join(notes) or "no change"))

    # --- IBKR: resolution, entries, exits ---------------------------------------------------------------
    def resolve(self, broker) -> int:
        n = 0
        for o in self.st.offers(WAITING_STATES):
            if o.get("con_id"):
                continue
            c, how = broker.resolve([t for t, _ in o.get("tickers") or []], o.get("cusip"))
            reasons = offer.contract_eligibility(c, self.cfg) if c else [how]
            if reasons:
                self.st.update(o["id"], "INELIGIBLE", reasons=reasons, contract=c, decided_at=self.now.isoformat())
                self.st.event(o["id"], "resolve_failed", reasons=reasons, contract=c)
                self.nt.send(f"{o['name']}: not tradable", "; ".join(reasons))
            else:
                self.st.update(o["id"], con_id=c["con_id"], symbol=c["symbol"], contract=c, match=how)
                self.st.event(o["id"], "resolved", contract=c, match=how)
            n += 1
        return n

    def _open_value(self) -> float:
        return sum(float(o.get("qty_held") or 0) * float(o.get("entry_price") or 0) for o in self.st.offers(OPEN_STATES))

    def entries(self, broker, placing: bool) -> list[str]:
        """Buy for offers whose entry close is today (or within the catch-up allowance)."""
        out = []
        t = self.now.time()
        if not (datetime.strptime("15:30", "%H:%M").time() <= t <= datetime.strptime("16:00", "%H:%M").time()):
            log.warning("entries at %s ET: outside the close window (15:30-16:00)", t.strftime("%H:%M"))
        states = WAITING_STATES if placing else ("WATCH",)
        for o in self.st.offers(states):
            if not o.get("con_id"):
                continue
            status = offer.entry_status(o, self.today, self.cfg)
            if status == "early":
                continue
            if status == "late":
                self._skip(o, f"entry window passed (entry day {o['entry_day']}, catch-up {self.cfg['entry_catchup_days']})")
                continue
            reasons = offer.edgar_eligibility(dict(o, entry_day=self.today.isoformat()), self.cfg, self.today)
            if reasons:
                self._skip(o, "; ".join(reasons))
                continue
            if len(self.st.offers(OPEN_STATES)) >= self.cfg["max_open_positions"]:
                self._skip(o, f"max_open_positions {self.cfg['max_open_positions']} reached")
                continue
            q = broker.quote(o["con_id"])
            if q.get("ask") is None or q.get("price_type") not in ("live", "delayed"):
                self.st.event(o["id"], "no_quote", quote=q)        # a data gap is not a verdict: retry next run
                self.nt.send(f"no quote for {o.get('symbol')}", f"no ask ({q.get('price_type')}, error "
                             f"{q.get('error')}); entry retried on the next entry run within the catch-up allowance")
                continue
            guard = offer.price_guard(q.get("ask"), o, self.cfg)
            if guard:
                self._skip(o, f"price guard: {guard}", quote=q)
                continue
            held, pending = broker.holdings(o["con_id"])
            ext = self.cfg["external_holdings"].get((o.get("symbol") or "").upper(), 0)
            n, why = offer.shares_to_buy(held, pending, ext, self.cfg)
            if why:
                self._skip(o, why)
                continue
            value = n * q["ask"]
            if value > self.cfg["max_trade_value"]:
                self._skip(o, f"{n} x ${q['ask']:.2f} = ${value:,.0f} exceeds max_trade_value")
                continue
            if self._open_value() + value > self.cfg["max_total_value"]:
                self._skip(o, f"max_total_value ${self.cfg['max_total_value']:,.0f} would be exceeded")
                continue
            spy = broker.benchmark()
            r = broker.order(o["con_id"], n, q, idem=f"{o['id']}:buy:{self.today}")
            self.st.event(o["id"], "entry_order", result=r, quote=q, spy=spy)
            out.append(f"{o.get('symbol')}: {r['mode']} {r.get('status') or ''}".strip())
            if r["mode"] == "dry-run":
                self.st.update(o["id"], "DRYRUN", dry_run={"qty": n, "limit": r["limit"], "ask": q["ask"], "at": q.get("at")},
                               decided_at=self.now.isoformat())
                self.nt.send(f"DRY RUN: would buy {n} {o.get('symbol')}", f"limit ${r['limit']:.2f} (ask ${q['ask']:.2f}); "
                             f"{describe(o)}", "low")
            elif r["mode"] == "blocked" or r.get("refused"):
                why = "; ".join(r["blocked"] + r["refused"])
                self.nt.send(f"entry blocked: {o.get('symbol')}", why, "critical")
            elif r.get("filled"):
                px = float(r.get("avg_price") or r["limit"])
                o = self.st.update(o["id"], "HELD", qty_held=r["filled"], entry_price=px, entry_at=self.now.isoformat(),
                                   decided_at=self.now.isoformat(),
                                   entry_mode=r["mode"], commission_est=PRO_FIXED.estimate(r["filled"], px), spy_entry=spy)
                self.nt.send(f"bought {int(r['filled'])} {o['symbol']} at ${px:.2f} ({r['mode']}): TENDER NEEDED",
                             tender_instructions(o), "critical")
            else:
                self.nt.send(f"entry not filled: {o.get('symbol')}", f"{r.get('status')} {r.get('error') or ''}; "
                             "will retry on the next entry run if within the catch-up allowance")
        return out

    def _skip(self, o: dict, why: str, **extra) -> None:
        self.st.update(o["id"], "SKIPPED", reasons=[why], decided_at=self.now.isoformat())
        self.st.event(o["id"], "skipped", reason=why, **extra)
        self.nt.send(f"skipped {o.get('symbol') or o['name']}", why, "low")

    def exits(self, broker) -> list[str]:
        """Sell on termination or on request, sell unaccepted shares after payment, close finished offers."""
        out = []
        for o in self.st.offers(("SELL_REQUESTED", "EXIT_PENDING", "AWAIT_PAYMENT")):
            held, _ = broker.holdings(o["con_id"])
            if o["state"] == "AWAIT_PAYMENT":
                if self.today < date.fromisoformat(o["payment_expected"]):
                    continue
                if o.get("simulated_tender") or (held > 0 and o.get("outcome") == "completed" and broker.is_paper()):
                    out.append(self._paper_tender(o, broker, held))
                    continue
                if held == 0:
                    self._close(o, broker, "paid")
                    out.append(f"{o['symbol']}: closed (paid)")
                    continue
                if held >= float(o.get("qty_held") or 0) and o.get("oddlot_priority"):
                    self.nt.send(f"{o['symbol']}: no shares taken up", f"{held:g} shares still held after the "
                                 "results: check the election in IBKR.", "critical")
                if not self.cfg["auto_sell_remainder"]:
                    continue
            elif held == 0:
                n = len(self.st.events(o["id"], "no_shares_to_sell"))
                self.st.event(o["id"], "no_shares_to_sell")
                if n >= 4:
                    self.nt.send(f"{o['symbol']}: nothing to sell", "no position visible for 5 runs (tendered shares "
                                 "may still be held by the agent); check IBKR", "critical")
                continue
            q = broker.quote(o["con_id"])
            r = broker.order(o["con_id"], -int(held), q, idem=f"{o['id']}:sell:{self.today}:{self.now:%H%M}")
            self.st.event(o["id"], "sell_order", result=r, quote=q, reason=o["state"])
            out.append(f"{o['symbol']}: sell {r['mode']} {r.get('status') or ''}".strip())
            if r.get("filled"):
                self.st.event(o["id"], "sell_fill", qty=r["filled"], price=float(r.get("avg_price") or r["limit"]),
                              commission_est=PRO_FIXED.estimate(r["filled"], float(r.get("avg_price") or r["limit"])))
                if r["filled"] >= held:
                    self._close(o, broker, "sold" if o["state"] != "AWAIT_PAYMENT" else "paid + remainder sold")
            elif r["mode"] in ("blocked",) or r.get("refused"):
                self.nt.send(f"sell blocked: {o['symbol']}", "; ".join(r["blocked"] + r["refused"]), "critical")
        return out

    def _paper_tender(self, o: dict, broker, held: float) -> str:
        """IBKR paper accounts cannot make voluntary corporate-action elections, so tendered shares are never taken
        up there. Treat every share held (up to qty_held) as accepted at the final price (the rule pnl() uses), then
        flatten the paper position. That sale is recorded as ``paper_cleanup_fill``: kept in the event log for
        reconciliation with the broker, excluded from the P&L. Live accounts never come here."""
        if not o.get("simulated_tender"):
            qty = min(float(held), float(o.get("qty_held") or 0))
            price = tender_price(o)
            self.st.event(o["id"], "paper_tender_simulated", qty=qty, price=price, held=held,
                          tender_confirmed=bool(o.get("tendered_at")))
            o = self.st.update(o["id"], simulated_tender=True)
            if price is None:
                log.warning("%s: no final price parsed; the simulated tender is valued at 0", o.get("symbol"))
        if held <= 0:
            self._close(o, broker, CLOSE_SIMULATED)
            return f"{o['symbol']}: closed (paper tender simulated)"
        q = broker.quote(o["con_id"])
        r = broker.order(o["con_id"], -int(held), q, idem=f"{o['id']}:paper_cleanup:{self.today}:{self.now:%H%M}")
        self.st.event(o["id"], "paper_cleanup_order", result=r, quote=q)
        if r.get("filled"):
            px = float(r.get("avg_price") or r["limit"])
            self.st.event(o["id"], "paper_cleanup_fill", qty=r["filled"], price=px,
                          commission_est=PRO_FIXED.estimate(r["filled"], px))
            if r["filled"] >= held:
                self._close(o, broker, CLOSE_SIMULATED)
                return f"{o['symbol']}: closed (paper tender simulated, cleanup {r['mode']})"
        elif r["mode"] in ("blocked",) or r.get("refused"):
            self.nt.send(f"paper cleanup blocked: {o['symbol']}", "; ".join(r["blocked"] + r["refused"]), "critical")
        return f"{o['symbol']}: paper cleanup {r['mode']} {r.get('status') or ''}".strip()

    def _close(self, o: dict, broker, how: str) -> None:
        spy = broker.benchmark() if broker else None
        o = self.st.update(o["id"], "CLOSED", closed_at=self.now.isoformat(), close_how=how, spy_exit=spy)
        p = pnl(o, self.st.events(o["id"]))
        self.st.update(o["id"], pnl=p)
        self.st.event(o["id"], "closed", how=how, pnl=p)
        self.nt.send(f"closed {o['symbol']} ({how})", format_pnl(p))

    # --- deadlines and reminders ------------------------------------------------------------------------
    def deadlines(self, broker=None) -> list[str]:
        out = []
        for o in self.st.offers(("HELD", "TENDERED")):
            exp = date.fromisoformat(o.get("expire_final") or o["expire"])
            dl = timing.ibkr_deadline(exp)
            left = timing.trading_days_between(self.today, dl.date())
            cap = offer.cap_price(o)
            bid = None
            if broker is not None:
                try:
                    bid = broker.quote(o["con_id"]).get("bid")
                except Exception as e:  # noqa: BLE001 - reminders must still go out
                    log.warning("quote for %s failed: %s", o.get("symbol"), e)
            above = bid is not None and cap is not None and bid > cap
            if broker is not None:
                self._check_position(o, broker)
            utc_today = datetime.now(timezone.utc).date().isoformat()
            sent_today = any(e["at"][:10] == utc_today for e in self.st.events(o["id"], "reminder"))
            if o["state"] == "HELD":
                if self.now > dl:
                    if not self.st.events(o["id"], "missed_deadline"):
                        self.st.event(o["id"], "missed_deadline")
                        self.nt.send(f"{o['symbol']}: IBKR tender deadline passed", f"Deadline was {dl:%Y-%m-%d %H:%M} ET. "
                                     "Late instructions are best-efforts only: send an IBKR web ticket now if you "
                                     f"want to try, then tender_bot confirm {o['id']}; or tender_bot sell {o['id']}.",
                                     "critical")
                    continue
                if left <= self.cfg["reminder_business_days"] and not sent_today:
                    msg = tender_instructions(o)
                    if above:
                        msg = (f"BID ${bid:.2f} IS ABOVE THE OFFER CAP ${cap:.2f}: the rule sells in the market "
                               f"instead of tendering: tender_bot sell {o['id']}\n\n") + msg
                        if self.cfg["auto_sell_above_offer"]:
                            self.st.update(o["id"], "SELL_REQUESTED")
                            msg = "auto_sell_above_offer is on: queued a market sale.\n" + msg
                    self.st.event(o["id"], "reminder", left=left, bid=bid, cap=cap)
                    self.nt.send(f"{o['symbol']}: tender reminder ({left} trading days to IBKR deadline)", msg,
                                 "critical" if left == 0 else "normal")
                    out.append(f"{o['symbol']}: reminder")
            elif above and left <= self.cfg["reminder_business_days"] and not sent_today:
                self.st.event(o["id"], "reminder", left=left, bid=bid, cap=cap)
                self.nt.send(f"{o['symbol']}: bid above offer after you tendered",
                             f"Bid ${bid:.2f} > cap ${cap:.2f}. Selling instead needs the election withdrawn first "
                             "(tender offers allow withdrawal until expiration; at IBKR via a web ticket).")
            if self.today > exp + timedelta(days=14) and not o.get("outcome"):
                if not any(e["at"][:10] >= (self.today - timedelta(days=5)).isoformat() for e in self.st.events(o["id"], "no_results")):
                    self.st.event(o["id"], "no_results")
                    self.nt.send(f"{o['symbol']}: no results filing found", "Expired over two weeks ago with no "
                                 "SC TO-I/A results parsed: check the issuer's 8-K / press release and IBKR.")
        return out

    def _check_position(self, o: dict, broker) -> None:
        """Alert when shares the bot bought have changed without a bot order: another strategy trading the same
        account (e.g. a rebalancer that zeroes symbols it doesn't hold a weight in) or a manual trade. For a
        TENDERED offer IBKR may also move elected shares out of the visible position."""
        try:
            held, _ = broker.holdings(o["con_id"])
        except Exception as e:  # noqa: BLE001
            log.warning("holdings for %s failed: %s", o.get("symbol"), e)
            return
        want = float(o.get("qty_held") or 0)
        if held == want or self.st.events(o["id"], "position_changed"):
            return
        self.st.event(o["id"], "position_changed", held=held, expected=want)
        why = ("IBKR may have moved the elected shares after your tender; check the account" if o["state"] == "TENDERED"
               else "another strategy or a manual trade changed it: the tender needs all shares held until expiry")
        self.nt.send(f"{o['symbol']}: position is {held:g}, expected {want:g}", why,
                     "normal" if o["state"] == "TENDERED" else "critical")
        if held >= 100:
            self.nt.send(f"{o['symbol']}: {held:g} shares held", "100 or more shares loses odd-lot priority", "critical")

    # --- user actions -----------------------------------------------------------------------------------
    def confirm(self, oid: str) -> dict:
        o = self.st.get(oid)
        if o["state"] != "HELD":
            raise ValueError(f"{oid} is {o['state']}, not HELD")
        self.st.event(oid, "tender_confirmed")
        return self.st.update(oid, "TENDERED", tendered_at=self.now.isoformat())

    def request_sell(self, oid: str) -> dict:
        o = self.st.get(oid)
        if o["state"] not in ("HELD", "TENDERED", "AWAIT_PAYMENT"):
            raise ValueError(f"{oid} is {o['state']}: nothing to sell")
        self.st.event(oid, "sell_requested", previous=o["state"])
        return self.st.update(oid, "SELL_REQUESTED")


def tender_price(o: dict) -> float | None:
    """The price tendered shares are paid: the final price from the results filing; for a fixed offer without a
    parsed final price, the offer price; for a Dutch auction without one, unknown."""
    return o.get("final_price") or (None if o.get("dutch") else offer.cap_price(o))


def pnl(o: dict, events: list[dict]) -> dict:
    """Per-event P&L. Tender proceeds are estimated from the results filing (accepted = held - sold, at the final
    price; for a fixed offer without a parsed final price, the offer price): verify against the IBKR statement.
    In paper the accepted quantity is the simulated one (``paper_tender_simulated``); the paper cleanup sale that
    follows is not part of the P&L."""
    qty = float(o.get("qty_held") or 0)
    cost = qty * float(o.get("entry_price") or 0) + float(o.get("commission_est") or 0)
    sold = [e for e in events if e["kind"] == "sell_fill"]      # paper_cleanup_fill is deliberately not here
    sold_qty = sum(float(e["qty"]) for e in sold)
    sold_val = sum(float(e["qty"]) * float(e["price"]) - float(e.get("commission_est") or 0) for e in sold)
    sim = [e for e in events if e["kind"] == "paper_tender_simulated"] if o.get("simulated_tender") else []
    if sim:
        tendered = float(sim[0]["qty"])
    else:
        tendered = max(qty - sold_qty, 0.0) if o.get("outcome") == "completed" else 0.0
    price = tender_price(o)
    proceeds = sold_val + tendered * float(price or 0)
    p = dict(qty=qty, cost=round(cost, 2), sold_qty=sold_qty, tendered_qty=tendered, tender_price=price,
             proceeds=round(proceeds, 2), pnl=round(proceeds - cost, 2),
             ret=round(proceeds / cost - 1, 5) if cost else None, estimated=bool(tendered),
             simulated_tender=bool(sim))
    s0, s1 = o.get("spy_entry"), o.get("spy_exit")
    if s0 and s1 and p["ret"] is not None:
        p["spy_ret"] = round(s1 / s0 - 1, 5)
        p["mkt_adj"] = round(p["ret"] - p["spy_ret"], 5)
    if o.get("entry_at") and o.get("closed_at"):
        p["days"] = (datetime.fromisoformat(o["closed_at"]) - datetime.fromisoformat(o["entry_at"])).days
    return p


def format_pnl(p: dict) -> str:
    s = f"cost ${p['cost']:,.2f} -> proceeds ${p['proceeds']:,.2f}: P&L ${p['pnl']:,.2f}"
    if p.get("ret") is not None:
        s += f" ({p['ret']:+.2%}"
        s += f", SPY {p['spy_ret']:+.2%}, market-adjusted {p['mkt_adj']:+.2%})" if "mkt_adj" in p else ")"
    if p.get("simulated_tender"):
        s += "\npaper account: the tender was SIMULATED (paper cannot elect); the cleanup sale is excluded"
    if p.get("estimated"):
        s += f"\ntender proceeds estimated: {p['tendered_qty']:g} x ${float(p['tender_price'] or 0):.2f} (check the IBKR statement)"
    return s
