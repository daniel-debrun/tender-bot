"""The parser on hand-checked SC TO-I filings (fixtures/expected.json holds the verified fields)."""
import json
from pathlib import Path

import pytest

from conftest import FIX
from tender_bot import amend, offer, parse

EXP = json.loads((FIX / "expected.json").read_text())


@pytest.mark.parametrize("acc", sorted(EXP["originals"]))
def test_original_fields(acc):
    want = EXP["originals"][acc]
    h, d = offer.read(FIX / f"{acc}.txt.gz")
    got = offer.from_original(h, d)
    assert h["form"] == "SC TO-I" and h["subj_cik"] == want["cik"]
    for k in ("sclass", "exchange", "dutch", "lo", "hi", "fixed", "oddlot_priority", "any_all", "oddlot_only", "expire"):
        assert got[k] == want[k], (k, got[k], want[k])
    assert got["sources"]["sclass"] == "regex"


@pytest.mark.parametrize("acc", sorted(EXP["amendments"]))
def test_amendment_fields(acc):
    want = EXP["amendments"][acc]
    _, d = offer.read(FIX / f"{acc}.txt.gz")
    got = amend.read_amendment(d)
    for k in ("terminated", "results_strict", "results", "final_price", "proration"):
        assert got[k] == pytest.approx(want[k]) if isinstance(want[k], float) else got[k] == want[k], (k, got[k], want[k])


def test_acceptance_time_read_from_header():
    h, _ = offer.read(FIX / "0000930413-14-000147.txt.gz")
    assert h["accepted"] == "20140114071956"
    assert offer.parse_accepted(h["accepted"]).isoformat() == "2014-01-14T07:19:56-05:00"


def test_conditional_extension_is_not_an_extension():
    # WebMD 2010 participant letter: "unless the Offer is extended" is conditional, so no extension
    _, d = offer.read(FIX / "0000950123-10-076145.txt.gz")
    a = amend.read_amendment(d)
    assert a["new_expire"] == "2010-10-01" and a["extension"] is None


def test_affirmative_extension():
    t = ("The Company has extended the Expiration Date of the Offer from November 8, 2023 at 8:00 a.m., New York City "
         "time, to November 16, 2023 at 8:00 a.m., New York City time, unless the Company extends it further.")
    assert amend.extension(t)[0] == "2023-11-16"
    assert amend.extension("The Offer will expire on May 1, 2024, unless the Offer is extended.")[0] is None
