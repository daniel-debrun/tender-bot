from datetime import date, datetime

import pytest

from tender_bot import config, offer, timing


def base(**kw):
    o = dict(sclass="common", exchange=False, dutch=True, lo=8.5, hi=9.25, hi_final=9.25, fixed=None,
             oddlot_priority=True, any_all=False, oddlot_only=False, expire="2014-02-11", entry_day="2014-01-14")
    o.update(kw)
    return o


def test_eligible(cfg):
    assert offer.edgar_eligibility(base(), cfg, date(2014, 1, 14)) == []


@pytest.mark.parametrize("kw,why", [
    (dict(sclass="warrants"), "class"), (dict(exchange=True), "exchange offer"),
    (dict(oddlot_priority=False), "odd-lot"), (dict(oddlot_only=True), "odd-lot-only"),
    (dict(dutch=False, fixed=None, hi=None, hi_final=None), "price"), (dict(expire=None), "expiration"),
    (dict(expire="2014-01-16"), "trading days after entry"),
])
def test_ineligible(cfg, kw, why):
    r = offer.edgar_eligibility(base(**kw), cfg, date(2014, 1, 14))
    assert any(why in x for x in r), r


def test_any_and_all_without_oddlot_clause(cfg):
    assert offer.edgar_eligibility(base(oddlot_priority=False, any_all=True), cfg, date(2014, 1, 14)) == []
    cfg["allow_any_and_all"] = False
    assert offer.edgar_eligibility(base(oddlot_priority=False, any_all=True), cfg, date(2014, 1, 14))


def test_oddlot_requirement_is_a_flag(cfg):
    cfg["require_oddlot_provision"] = False
    assert offer.edgar_eligibility(base(oddlot_priority=False), cfg, date(2014, 1, 14)) == []


def test_contract_rules(cfg):
    ok = dict(stock_type="COMMON", primary_exchange="NASDAQ", currency="USD")
    assert offer.contract_eligibility(ok, cfg) == []
    assert offer.contract_eligibility(dict(ok, stock_type="CLOSED-END FUND"), cfg)
    assert offer.contract_eligibility(dict(ok, primary_exchange="PINK"), cfg)
    assert offer.contract_eligibility(None, cfg)


def test_price_guard(cfg):
    o = base()
    assert offer.price_guard(9.10, o, cfg) is None
    assert "ask 9.25 >= cap 9.25" in offer.price_guard(9.25, o, cfg)
    assert offer.price_guard(None, o, cfg) == "no ask quote"
    fixed = base(dutch=False, fixed=52.0, price_change=53.0)
    assert offer.cap_price(fixed) == 53.0
    assert offer.price_guard(52.5, fixed, cfg) is None
    cfg["min_edge"] = 0.01
    assert offer.price_guard(52.5, fixed, cfg)


def test_99_share_guard(cfg):
    assert offer.shares_to_buy(0, 0, 0, cfg) == (99, None)
    for held, pend, ext in ((1, 0, 0), (0, 99, 0), (0, 0, 5)):
        n, why = offer.shares_to_buy(held, pend, ext, cfg)
        assert n == 0 and "already hold" in why


def test_config_rejects_unknown_and_100_shares(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"shares": 100}')
    with pytest.raises(config.ConfigError):
        config.load(p)
    p.write_text('{"ibkr": {"alow_live": true}}')
    with pytest.raises(config.ConfigError, match="ibkr.alow_live"):
        config.load(p)


def test_entry_day():
    ET = timing.ET
    assert timing.entry_day(datetime(2014, 1, 14, 7, 19, tzinfo=ET), date(2014, 1, 14)) == date(2014, 1, 14)
    assert timing.entry_day(datetime(2014, 1, 14, 16, 30, tzinfo=ET), date(2014, 1, 14)) == date(2014, 1, 15)
    assert timing.entry_day(datetime(2026, 10, 16, 17, 0, tzinfo=ET), date(2026, 10, 16)) == date(2026, 10, 19)
    assert timing.entry_day(None, date(2026, 10, 16)) == date(2026, 10, 19)          # next day


def test_ibkr_deadline():
    # expires Tuesday 2014-02-11 -> noon ET Monday 2014-02-10; expires Monday after a holiday weekend
    assert timing.ibkr_deadline(date(2014, 2, 11)) == datetime(2014, 2, 10, 12, 0, tzinfo=timing.ET)
    assert timing.ibkr_deadline(date(2026, 1, 20)).date() == date(2026, 1, 16)      # MLK day is 2026-01-19


def test_entry_status(cfg):
    o = base(entry_day="2026-10-16")
    assert offer.entry_status(o, date(2026, 10, 15), cfg) == "early"
    assert offer.entry_status(o, date(2026, 10, 16), cfg) == "due"
    assert offer.entry_status(o, date(2026, 10, 19), cfg) == "due"      # 1 catch-up trading day
    assert offer.entry_status(o, date(2026, 10, 20), cfg) == "late"


def test_heartbeats_command(capsys):
    from tender_bot import cli

    assert cli.main(["heartbeats"]) == 1                     # no job has run in the isolated IBKR_OPS_STATE
    assert "tender-scan: never ran" in capsys.readouterr().out
