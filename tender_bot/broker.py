"""IBKR side through ibkr-ops: contract resolution, quotes, holdings across accounts, and gated orders.

Orders always go through ``ibkr_ops.orders.execute`` (gates: dry run unless placing, paper only unless live is
authorised three ways, kill switch, caps, fresh live quote, bounded limit, idempotency key, what-if). Orders are
keyed by pinned contract (``CON:<conId>``) and the policy allows only that security type and that one key."""
from __future__ import annotations

import logging
from datetime import datetime

from ibkr_ops import gates, orders
from ibkr_ops.config import is_paper_account
from ibkr_ops.contracts import to_contract

log = logging.getLogger("tender_bot")


def ib_symbol(sec_ticker: str) -> str:
    """SEC ticker -> IB symbol: class shares are 'BRK-B' at the SEC and 'BRK B' at IB."""
    return sec_ticker.upper().replace("-", " ").replace(".", " ")


def isin_cusip(isin: str | None) -> str | None:
    return isin[2:10] if isin and len(isin) == 12 and isin[:2] in ("US", "CA") else None


class Broker:
    def __init__(self, session, cfg: dict, place: bool = False, allow_live: bool = False,
                 ledger: str | None = None, kill_switch: str | None = None):
        self.s, self.cfg = session, cfg
        self.place, self.allow_live = place, allow_live
        self.ledger, self.kill_switch = ledger, kill_switch

    # --- reads -------------------------------------------------------------------------------------------
    def resolve(self, sec_tickers: list[str], cusip: str | None) -> tuple[dict | None, str]:
        """The IBKR stock for an issuer's SEC tickers, matched on CUSIP (via ISIN) when the cover page gives one.
        Returns (contract info, how it matched or why it failed)."""
        from ib_async import Stock

        cands = []
        for t in dict.fromkeys(ib_symbol(t) for t in sec_tickers):
            for d in self.s.contract_details(Stock(t, "SMART", "USD")) or []:
                c = d.contract
                isin = next((x.value for x in (d.secIdList or []) if x.tag == "ISIN"), None)
                cands.append(dict(con_id=c.conId, symbol=c.symbol, primary_exchange=c.primaryExchange,
                                  currency=c.currency, stock_type=d.stockType, long_name=d.longName, isin=isin))
        if not cands:
            return None, f"no IBKR contract for {sec_tickers}"
        if cusip:
            hit = [c for c in cands if isin_cusip(c["isin"]) == cusip[:8].upper()]
            if len(hit) == 1:
                return hit[0], "cusip"
            if any(c["isin"] for c in cands):
                return None, f"no IBKR listing matches CUSIP {cusip} (ISINs {[c['isin'] for c in cands]})"
        common = [c for c in cands if c["stock_type"] == "COMMON"]
        if len(common) == 1:
            return common[0], "single common listing"
        return None, f"ambiguous: {len(cands)} IBKR listings, {len(common)} common"

    def quote(self, con_id: int) -> dict:
        from ib_async import Contract

        return self.s.quotes({"q": Contract(conId=int(con_id), exchange="SMART")}, wait=6)["q"]

    def is_paper(self) -> bool:
        """True when every account this login manages is a paper account (``DU``/``DF``: ibkr-ops's rule)."""
        accounts = self.s.accounts
        return bool(accounts) and all(is_paper_account(a) for a in accounts)

    def benchmark(self) -> float | None:
        """SPY price (mid, else last) for the market-adjusted ledger; None if unavailable."""
        try:
            q = self.s.quotes({"SPY": to_contract("SPY")}, wait=4)["SPY"]
        except Exception as e:  # noqa: BLE001 - benchmark is best effort
            log.warning("SPY quote failed: %s", e)
            return None
        b, a = q.get("bid"), q.get("ask")
        return (b + a) / 2 if b and a else q.get("last")

    def holdings(self, con_id: int) -> tuple[float, float]:
        """(shares held in every account this login manages, shares in open buy orders from any client)."""
        held = sum(float(p.position) for p in self.s.ib.reqPositions() if p.contract.conId == int(con_id))
        pending = sum(float(t.remaining()) for t in self.s.ib.reqAllOpenOrders()
                      if t.contract.conId == int(con_id) and t.order.action == "BUY")
        return held, pending

    # --- orders ------------------------------------------------------------------------------------------
    def _policy(self, key: str, value_cap: float, daily_cap: float | None) -> gates.Policy:
        ib = self.cfg["ibkr"]
        return gates.Policy(place=self.place, allow_live=self.allow_live, kill_switch=self.kill_switch,
                            sec_types=frozenset({"CON"}), allowlist=frozenset({key}),
                            max_order_value=value_cap, max_batch_value=value_cap, max_daily_value=daily_cap,
                            max_orders=1, max_slippage=self.cfg["slippage"] + 1e-9,
                            max_quote_age_s=ib["max_quote_age_s"], allow_delayed_quotes=ib["allow_delayed_quotes"])

    def order(self, con_id: int, qty: int, q: dict, idem: str) -> dict:
        """Buy (qty > 0) at a marketable limit over the ask, or sell (qty < 0) under the bid, through the gates.
        Returns {mode, status, filled, avg_price, limit, error, blocked, refused}."""
        key = f"CON:{int(con_id)}"
        ref = q.get("ask") if qty > 0 else q.get("bid")
        limit = gates.marketable_limit(ref, qty, self.cfg["slippage"]) if ref else None
        at = datetime.fromisoformat(q["at"]) if q.get("at") else None
        intent = gates.OrderIntent(key=key, qty=qty, limit=limit, ref_price=ref, quote_at=at,
                                   price_type=q.get("price_type"), idem_key=idem)
        if qty > 0:
            policy = self._policy(key, float(self.cfg["max_trade_value"]), self.cfg["ibkr"]["max_daily_value"])
        else:   # never let an entry cap trap an exit: allow the position's current value
            policy = self._policy(key, max(float(self.cfg["max_trade_value"]), abs(qty) * (ref or 0) * 1.05), None)
        decision, results = orders.execute(self.s, [intent], policy, ledger=self.ledger,
                                           rest_seconds=self.cfg["rest_seconds"])
        out = dict(mode=decision.mode, limit=limit, ref=ref, blocked=decision.blocked,
                   refused=[why for _, why in decision.refused])
        r = results.get(idem)
        if r:
            out.update(status=r.get("status"), filled=float(r.get("filled") or 0), avg_price=r.get("avg_price"),
                       error=r.get("error"), order_id=r.get("order_id"))
        return out
