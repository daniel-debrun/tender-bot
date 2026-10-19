"""Trading-calendar timing (US Eastern time, NYSE trading days from ibkr-ops)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ibkr_ops.tradingdays import is_trading_day

ET = ZoneInfo("America/New_York")
CLOSE = time(16, 0)
IBKR_DEADLINE = time(12, 0)    # IBKR: US instructions by 12:00 ET the day before the offer expires


def now_et() -> datetime:
    return datetime.now(ET)


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def add_trading_days(d: date, n: int) -> date:
    for _ in range(n):
        d = next_trading_day(d)
    return d


def trading_days_between(a: date, b: date) -> int:
    """Trading days after ``a`` up to and including ``b`` (0 if b <= a)."""
    n, d = 0, a
    while d < b:
        d = next_trading_day(d)
        if d <= b:
            n += 1
    return n


def entry_day(accepted: datetime | None, filed: date) -> date:
    """Entry rule: the close of the first session that closes after acceptance. Accepted before 16:00 ET
    on a trading day d -> d; otherwise the next trading day. Unknown acceptance time -> the trading day after
    the filing date (conservative)."""
    if accepted is None:
        return next_trading_day(filed)
    a = accepted.astimezone(ET)
    d = a.date()
    if is_trading_day(d) and a.time() < CLOSE:
        return d
    return next_trading_day(d)


def ibkr_deadline(expire: date) -> datetime:
    """IBKR's election deadline: 12:00 ET on the trading day before expiration."""
    return datetime.combine(prev_trading_day(expire), IBKR_DEADLINE, ET)
