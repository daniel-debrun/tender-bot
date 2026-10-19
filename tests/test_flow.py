"""End to end with a fake EDGAR and ibkr-ops's FakeIB behind a real ibkr_ops Session and real gates.

Wendy's 2014 Dutch auction ($8.50-$9.25, odd-lot priority, filed 2014-01-14 07:19 ET, expires 2014-02-11;
results amendment 2014-02-12: final $9.25) and Blue Wolf 2013 (terminated)."""
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("fake_ib")
from fake_ib import FakeIB, Script  # noqa: E402
from ib_async import Contract, ContractDetails, TagValue  # noqa: E402
from ibkr_ops.config import Endpoint  # noqa: E402
from ibkr_ops.session import Session  # noqa: E402

from conftest import FIX  # noqa: E402
from tender_bot import notify, store, timing  # noqa: E402
from tender_bot.bot import Bot  # noqa: E402
from tender_bot.broker import Broker  # noqa: E402

WEN, WEN_CIK, WEN_CON = "0000930413-14-000147", 30697, 1001
WEN_RESULTS = "0000950142-14-000362"
ET = timing.ET


class FakeEdgar:
    def __init__(self):
        self.feed, self.fetched = [], []

    def latest(self):
        return list(self.feed)

    def daily(self, d):
        return []

    def submissions(self, cik):
        return []

    def tickers(self):
        return {WEN_CIK: [("WEN", "Nasdaq")], 1517526: [("MNGL", "Nasdaq")]}

    def full_text_path(self, cik, acc):
        self.fetched.append(acc)
        return FIX / f"{acc}.txt.gz"


class IB(FakeIB):
    """FakeIB keyed by conId for pinned contracts, plus positions and open orders."""

    def reqMktData(self, c, generic, snapshot, regulatory):
        c2 = Contract(symbol=str(c.conId) if c.conId else c.symbol)
        return super().reqMktData(c2, generic, snapshot, regulatory)

    def cancelMktData(self, c):
        pass

    def reqPositions(self):
        return [SimpleNamespace(account=self.s.account, contract=SimpleNamespace(conId=k), position=v, avgCost=0)
                for k, v in self.s.positions.items()]

    def reqAllOpenOrders(self):
        return []

    def placeOrder(self, c, o):
        t = super().placeOrder(Contract(symbol=str(c.conId)), o)
        if t.orderStatus.status == "Filled":
            sign = 1 if o.action == "BUY" else -1
            self.s.positions[c.conId] = self.s.positions.get(c.conId, 0) + sign * float(o.totalQuantity)
        return t


def details(symbol, con_id, isin, stock_type="COMMON", exch="NASDAQ"):
    return ContractDetails(contract=Contract(conId=con_id, symbol=symbol, secType="STK", primaryExchange=exch,
                                             currency="USD"), stockType=stock_type, longName=symbol,
                           secIdList=[TagValue("ISIN", isin)])


def live(bid, ask):
    return dict(bid=bid, ask=ask, last=(bid + ask) / 2, marketDataType=1)


@pytest.fixture
def world(cfg):
    script = Script(account="DU0000000")
    script.positions = {}
    script.details = {"WEN": [details("WEN", WEN_CON, "US9505871005")]}
    script.quotes = {str(WEN_CON): live(9.08, 9.10), "SPY": live(183.0, 183.02)}
    sink = []
    st = store.Store(cfg["state_dir"] + "/tender.db")
    ed = FakeEdgar()

    def bot_at(y, m, d, hh, mm):
        return Bot(cfg, st, ed, notify.Notifier(cfg, sink=sink), now=datetime(y, m, d, hh, mm, tzinfo=ET))

    def session(place):
        return Session(Endpoint(port=4002), ib_factory=lambda: IB(script), check_port=False, readonly=not place)

    def broker(s, place, **kw):
        return Broker(s, cfg, place=place, ledger=cfg["state_dir"] + "/orders.jsonl",
                      kill_switch=cfg["state_dir"] + "/KILL", **kw)

    return SimpleNamespace(script=script, sink=sink, st=st, ed=ed, bot_at=bot_at, session=session, broker=broker, cfg=cfg)


def detect_wendys(w):
    w.ed.feed = [dict(form="SC TO-I", acc=WEN, cik=WEN_CIK, name="Wendy's Co", filed="2014-01-14",
                      accepted="2014-01-14T07:19:56-05:00")]
    bot = w.bot_at(2014, 1, 14, 9, 0)
    bot.scan()
    return bot


def test_detect_parse_and_alert(world):
    w = world
    detect_wendys(w)
    o = w.st.get(WEN)
    assert o["state"] == "WATCH" and o["entry_day"] == "2014-01-14" and o["cusip"] == "95058710"
    assert o["dutch"] and o["lo"] == 8.5 and o["hi"] == 9.25 and o["oddlot_priority"]
    assert "new tender offer" in w.sink[-1][0]
    detect_wendys(w)                                       # seen-accession store: processed once
    assert w.ed.fetched.count(WEN) == 1


def test_unlisted_issuer_is_not_downloaded(world):
    w = world
    w.ed.feed = [dict(form="SC TO-I", acc="0000000000-26-000001", cik=999, name="Interval Fund", filed="2026-10-16",
                      accepted="2026-10-16T10:00:00-04:00")]
    w.bot_at(2026, 10, 16, 10, 5).scan()
    assert w.st.get("0000000000-26-000001")["state"] == "INELIGIBLE" and not w.ed.fetched


def test_full_lifecycle_paper(world):
    w = world
    detect_wendys(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        assert bot.resolve(b) == 1
        assert w.st.get(WEN)["match"] == "cusip"
        bot.entries(b, placing=True)
    o = w.st.get(WEN)
    assert o["state"] == "HELD" and o["qty_held"] == 99 and o["entry_mode"] == "paper"
    assert w.script.placed[-1][2:4] == ("BUY", 99.0)
    title, body, _ = w.sink[-1]
    assert "TENDER NEEDED" in title and "Corporate Actions Manager" in body and "purchase price determined" in body
    assert "2014-02-10 12:00" in body                      # IBKR deadline: noon the trading day before expiry

    # reminders start 2 trading days before the deadline, quote check included
    bot = w.bot_at(2014, 2, 6, 9, 40)
    with w.session(place=False) as s:
        assert bot.deadlines(w.broker(s, False)) == ["WEN: reminder"]
        assert bot.deadlines(w.broker(s, False)) == []     # once a day
    bot.confirm(WEN)
    assert w.st.get(WEN)["state"] == "TENDERED"

    # results amendment -> AWAIT_PAYMENT; payment date 3 trading days after the results filing
    w.ed.feed = [dict(form="SC TO-I/A", acc=WEN_RESULTS, cik=WEN_CIK, filed="2014-02-12", accepted=None)]
    bot = w.bot_at(2014, 2, 12, 18, 0)
    bot.scan()
    o = w.st.get(WEN)
    assert o["state"] == "AWAIT_PAYMENT" and o["final_price"] == 9.25 and o["payment_expected"] == "2014-02-18"

    # the agent takes up the 99 shares: position 0 -> closed with an estimated P&L
    w.script.positions[WEN_CON] = 0
    bot = w.bot_at(2014, 2, 18, 10, 0)
    with w.session(place=True) as s:
        bot.exits(w.broker(s, place=True))
    o = w.st.get(WEN)
    assert o["state"] == "CLOSED"
    p = o["pnl"]
    assert p["tendered_qty"] == 99 and p["proceeds"] == pytest.approx(99 * 9.25)
    assert p["ret"] == pytest.approx(99 * 9.25 / (99 * o["entry_price"] + 1.0) - 1, abs=1e-5)


def test_dry_run_sends_nothing(world):
    w = world
    detect_wendys(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=False) as s:
        b = w.broker(s, place=False)
        bot.resolve(b)
        bot.entries(b, placing=False)
    assert w.st.get(WEN)["state"] == "DRYRUN" and not w.script.placed
    assert w.sink[-1][0].startswith("DRY RUN")


@pytest.mark.parametrize("setup,state,reason", [
    (lambda w: w.script.positions.update({WEN_CON: 5}), "SKIPPED", "already hold"),
    (lambda w: w.script.quotes.update({str(WEN_CON): live(9.25, 9.27)}), "SKIPPED", "price guard"),
    (lambda w: w.cfg.update(max_trade_value=500.0), "SKIPPED", "max_trade_value"),
])
def test_entry_guards(world, setup, state, reason):
    w = world
    detect_wendys(w)
    setup(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    o = w.st.get(WEN)
    assert o["state"] == state and reason in o["reasons"][0] and not w.script.placed


def test_kill_switch_and_live_account_block(world):
    w = world
    detect_wendys(w)
    (w.st.path.parent / "KILL").write_text("x")
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    assert not w.script.placed and "kill switch" in w.sink[-1][1]
    (w.st.path.parent / "KILL").unlink()
    w.script.account = "U0000000"                        # a live account behind the paper port
    with w.session(place=True) as s:
        bot.entries(w.broker(s, place=True), placing=True)
    assert not w.script.placed and w.st.get(WEN)["state"] == "WATCH"


def test_termination_sells(world):
    w = world
    detect_wendys(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    # treat Blue Wolf's termination amendment as if it were Wendy's (same CIK in the feed)
    w.ed.feed = [dict(form="SC TO-I/A", acc="0001144204-13-039264", cik=WEN_CIK, filed="2014-01-20", accepted=None)]
    w.bot_at(2014, 1, 20, 18, 0).scan()
    assert w.st.get(WEN)["state"] == "EXIT_PENDING"
    assert w.sink[-1][2] == "critical"
    bot = w.bot_at(2014, 1, 21, 10, 0)
    with w.session(place=True) as s:
        bot.exits(w.broker(s, place=True))
    assert w.script.placed[-1][2:4] == ("SELL", 99.0)
    o = w.st.get(WEN)
    assert o["state"] == "CLOSED" and o["pnl"]["sold_qty"] == 99
    assert not o["pnl"].get("simulated_tender") and not w.st.events(WEN, "paper_tender_simulated")


def test_missing_quote_is_retried_not_skipped(world):
    w = world
    detect_wendys(w)
    w.script.quotes[str(WEN_CON)] = dict(last=9.1, marketDataType=1)          # no bid/ask
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    assert w.st.get(WEN)["state"] == "WATCH" and not w.script.placed
    w.script.quotes[str(WEN_CON)] = live(9.08, 9.10)                          # next trading day: catch-up entry
    bot = w.bot_at(2014, 1, 15, 15, 50)
    with w.session(place=True) as s:
        bot.entries(w.broker(s, place=True), placing=True)
    assert w.st.get(WEN)["state"] == "HELD"


def test_position_changed_outside_the_bot(world):
    w = world
    detect_wendys(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    w.script.positions[WEN_CON] = 0                                           # e.g. a rebalancer sold it
    bot = w.bot_at(2014, 1, 16, 9, 40)
    with w.session(place=False) as s:
        bot.deadlines(w.broker(s, False))
    assert any("position is 0, expected 99" in t and u == "critical" for t, _, u in w.sink)


# --- paper accounts cannot elect: the tender is simulated at the payment date ----------------------------------
def to_payment(w, confirm=True):
    """Wendy's bought in paper, (optionally) confirmed, results filed: AWAIT_PAYMENT with the 99 shares still held."""
    detect_wendys(w)
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    if confirm:
        w.bot_at(2014, 2, 6, 9, 40).confirm(WEN)
    w.ed.feed = [dict(form="SC TO-I/A", acc=WEN_RESULTS, cik=WEN_CIK, filed="2014-02-12", accepted=None)]
    w.bot_at(2014, 2, 12, 18, 0).scan()
    assert w.st.get(WEN)["state"] == "AWAIT_PAYMENT" and w.script.positions[WEN_CON] == 99


def test_paper_tender_is_simulated_and_cleanup_excluded(world):
    w = world
    to_payment(w)
    bot = w.bot_at(2014, 2, 18, 10, 0)
    with w.session(place=True) as s:
        bot.exits(w.broker(s, place=True))
    o = w.st.get(WEN)
    assert o["state"] == "CLOSED" and o["simulated_tender"] is True
    sim = w.st.events(WEN, "paper_tender_simulated")
    assert len(sim) == 1 and sim[0]["qty"] == 99 and sim[0]["price"] == 9.25 and sim[0]["tender_confirmed"] is True
    fills = w.st.events(WEN, "paper_cleanup_fill")
    assert len(fills) == 1 and fills[0]["qty"] == 99 and fills[0]["price"] > 0
    assert not w.st.events(WEN, "sell_fill")
    assert w.script.placed[-1][2:4] == ("SELL", 99.0) and w.script.positions[WEN_CON] == 0
    p = o["pnl"]
    assert p["simulated_tender"] is True and p["tendered_qty"] == 99 and p["sold_qty"] == 0
    assert p["proceeds"] == pytest.approx(99 * 9.25)                         # the cleanup sale is not in the P&L
    assert p["ret"] == pytest.approx(99 * 9.25 / (99 * o["entry_price"] + 1.0) - 1, abs=1e-5)
    assert not any("no shares taken up" in t for t, _, _ in w.sink)


def test_paper_cleanup_unfilled_is_retried_without_resimulating(world):
    w = world
    to_payment(w, confirm=False)
    w.script.order_outcome[str(WEN_CON)] = "rest"
    with w.session(place=True) as s:
        w.bot_at(2014, 2, 18, 10, 0).exits(w.broker(s, place=True))
    assert w.st.get(WEN)["state"] == "AWAIT_PAYMENT"
    assert len(w.st.events(WEN, "paper_tender_simulated")) == 1
    assert w.st.events(WEN, "paper_tender_simulated")[0]["tender_confirmed"] is False
    w.script.order_outcome.pop(str(WEN_CON))
    with w.session(place=True) as s:
        w.bot_at(2014, 2, 18, 15, 50).exits(w.broker(s, place=True))
    o = w.st.get(WEN)
    assert o["state"] == "CLOSED" and len(w.st.events(WEN, "paper_tender_simulated")) == 1
    assert o["pnl"]["tendered_qty"] == 99 and o["pnl"]["simulated_tender"] is True


def test_live_account_never_simulates(world):
    w = world
    to_payment(w)
    w.script.account = "U0000000"                         # a live account (on the live port, so not blocked as a mismatch)
    with Session(Endpoint(port=4001), ib_factory=lambda: IB(w.script), check_port=False,
                 readonly=False) as s:
        w.bot_at(2014, 2, 18, 10, 0).exits(w.broker(s, place=True))
    o = w.st.get(WEN)
    assert not w.st.events(WEN, "paper_tender_simulated") and not o.get("simulated_tender")
    assert any("no shares taken up" in t and u == "critical" for t, _, u in w.sink)
    assert o["state"] == "AWAIT_PAYMENT" and w.script.positions[WEN_CON] == 99   # live not authorised: sell blocked


def test_scan_job_exits_nonzero_when_a_filing_fails(world, monkeypatch):
    from tender_bot import cli

    w = world
    w.ed.feed = [dict(form="SC TO-I", acc=WEN, cik=WEN_CIK, name="Wendy's Co", filed="2014-01-14",
                      accepted="2014-01-14T07:19:56-05:00")]

    def broken(cik, acc):
        raise OSError("EDGAR 503")
    monkeypatch.setattr(w.ed, "full_text_path", broken)
    bot = w.bot_at(2014, 1, 14, 9, 0)
    assert bot.scan()["failed"] == 1 and not w.st.is_seen(WEN)              # retried on the next scan
    monkeypatch.setattr(cli, "_make", lambda cfg, now=None: bot)
    monkeypatch.setattr(cli.probe, "port_open", lambda h, p: False)
    assert cli.main(["scan"]) == 4                                            # ibkr-ops PARTIAL: on the record
