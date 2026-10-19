"""tender_bot export: offers / outcomes / fills / events as flat JSON lists."""
import json

import pytest

pytest.importorskip("fake_ib")
from test_flow import WEN, WEN_CON, detect_wendys, live, to_payment, world  # noqa: E402,F401

from tender_bot import cli, export  # noqa: E402
from tender_bot.bot import CLOSE_SIMULATED  # noqa: E402

OUTCOME = ("close_how", "closed_date", "days", "cost", "proceeds", "ret", "spy_ret", "mkt_adj", "simulated_tender",
           "estimated")
FUND = "0000000000-26-000001"


def closed_paper_trade(w):
    to_payment(w)
    with w.session(place=True) as s:
        w.bot_at(2014, 2, 18, 10, 0).exits(w.broker(s, place=True))
    w.ed.feed = [dict(form="SC TO-I", acc=FUND, cik=999, name="Interval Fund DU0000000", filed="2014-02-18",
                      accepted="2014-02-18T10:00:00-05:00")]
    w.bot_at(2014, 2, 18, 10, 5).scan()


def load(d, name):
    return json.loads((d / name).read_text())


def test_export_files(world, tmp_path):
    w = world
    closed_paper_trade(w)
    out = tmp_path / "out"
    export.write(w.st, out)
    text = "".join((out / f).read_text() for f in export.FILES)
    assert "DU0000000" not in text and "DU0000000" not in text and "DU*******" in text   # account ids never present
    for f in export.FILES:
        rows = load(out, f)
        assert isinstance(rows, list) and rows
        assert all(not isinstance(v, (dict, list)) for r in rows for v in r.values())   # flat

    offers = {r["id"]: r for r in load(out, "offers.json")}
    o = offers[WEN]
    assert (o["state"], o["ticker"], o["cik"], o["dutch"], o["cap_price"], o["oddlot_priority"]) == \
        ("CLOSED", "WEN", 30697, True, 9.25, True)
    assert (o["filed"], o["expire"], o["expire_final"], o["entry_date"], o["qty_held"], o["outcome"]) == \
        ("2014-01-14", "2014-02-11", "2014-02-11", "2014-01-14", 99, "completed")
    assert o["decided_at"].startswith("2014-01-14T15:50") and o["skip_reason"] is None and o["entry_price"] > 0
    assert offers[FUND]["state"] == "INELIGIBLE" and offers[FUND]["skip_reason"] == "no exchange-listed ticker"

    (oc,) = load(out, "outcomes.json")
    assert oc["id"] == WEN and oc["simulated_tender"] is True and oc["close_how"] == CLOSE_SIMULATED
    assert oc["entry_date"] == "2014-01-14" and oc["closed_at"].startswith("2014-02-18T10:00") and oc["days"] == 34
    assert oc["proceeds"] == pytest.approx(99 * 9.25) and oc["estimated"] is True

    fills = load(out, "fills.json")
    assert [(f["id"], f["kind"], f["qty"]) for f in fills] == [(WEN, "entry", 99), (WEN, "paper_cleanup", 99)]
    assert all(f["price"] > 0 and f["commission_est"] > 0 and f["at"] for f in fills)


def test_events_long_format_is_point_in_time(world, tmp_path):
    w = world
    closed_paper_trade(w)
    export.write(w.st, tmp_path)
    rows = load(tmp_path, "events.json")
    assert all(list(r) == list(export.EVENT_COLUMNS) for r in rows)
    by = {(r["event_id"], r["kind"]): r for r in rows}
    assert sorted(by) == sorted([(WEN, "seen"), (WEN, "entry"), (WEN, "close"), (FUND, "seen")])
    seen, entry, close = by[WEN, "seen"], by[WEN, "entry"], by[WEN, "close"]
    assert seen["published"] == "2014-01-14T14:00:00+00:00"                  # detected at 09:00 ET
    assert seen["eligible"] is True and seen["state_at_publish"] == "WATCH" and seen["entry_price"] is None
    assert seen["entry_date"] is None and all(seen[k] is None for k in OUTCOME)
    assert entry["published"] == "2014-01-14T20:50:00+00:00" and entry["entry_date"] == "2014-01-14"
    assert entry["qty"] == 99 and entry["entry_price"] > 0 and entry["state_at_publish"] == "HELD"
    assert all(entry[k] is None for k in OUTCOME)                             # an entry row carries no outcome
    assert seen["published"] <= entry["published"] <= close["published"]      # never published before its entry
    assert close["published"] == "2014-02-18T15:00:00+00:00" and close["closed_date"] == "2014-02-18"
    assert close["simulated_tender"] is True and close["ret"] is not None and close["state_at_publish"] == "CLOSED"
    fund = by[FUND, "seen"]
    assert fund["eligible"] is False and fund["skip_reason"] == "no exchange-listed ticker"
    assert [r["published"] for r in rows] == sorted(r["published"] for r in rows)


def test_skip_after_detection_is_its_own_row(world, tmp_path):
    w = world
    detect_wendys(w)
    w.script.quotes[str(WEN_CON)] = live(9.25, 9.27)                         # price guard
    bot = w.bot_at(2014, 1, 14, 15, 50)
    with w.session(place=True) as s:
        b = w.broker(s, place=True)
        bot.resolve(b)
        bot.entries(b, placing=True)
    export.write(w.st, tmp_path)
    rows = {r["kind"]: r for r in load(tmp_path, "events.json")}
    assert sorted(rows) == ["seen", "skip"]
    assert rows["seen"]["eligible"] is True and rows["seen"]["skip_reason"] is None
    skip = rows["skip"]
    assert skip["published"] == "2014-01-14T20:50:00+00:00" and skip["eligible"] is False
    assert "price guard" in skip["skip_reason"] and skip["state_at_publish"] == "SKIPPED"
    assert load(tmp_path, "outcomes.json") == [] and load(tmp_path, "fills.json") == []


def test_cli_export_writes_to_out_or_the_cwd(world, tmp_path, monkeypatch):
    w = world
    closed_paper_trade(w)
    cfgfile = tmp_path / "c.json"
    cfgfile.write_text(json.dumps({"state_dir": w.cfg["state_dir"]}))
    out = tmp_path / "run-out"
    out.mkdir()
    assert cli.main(["--config", str(cfgfile), "export", "--out", str(out)]) == 0
    assert {p.name for p in out.iterdir()} == set(export.FILES)
    other = tmp_path / "explicit"
    assert cli.main(["--config", str(cfgfile), "export", "--out", str(other)]) == 0
    assert (other / "events.json").exists()
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--config", str(cfgfile), "export"]) == 0 and (tmp_path / "offers.json").exists()
