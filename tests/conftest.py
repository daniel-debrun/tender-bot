import copy
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
sys.path.insert(0, str(HERE.parent))
# ibkr-ops's own test double for ib_async.IB (returns ib_async's real Ticker/Trade/OrderStatus objects)
IBKR_OPS_TESTS = Path(os.environ.get("IBKR_OPS_TESTS", HERE.parent.parent / "ibkr-ops" / "tests"))
if IBKR_OPS_TESTS.exists():
    sys.path.insert(0, str(IBKR_OPS_TESTS))
    sys.dont_write_bytecode = True        # don't leave __pycache__ in the ibkr-ops checkout


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("IBKR_OPS_STATE", str(tmp_path / "ibkr-ops-state"))
    monkeypatch.setenv("TENDER_BOT_USER_AGENT", "tender-bot-tests tests@example.com")
    for k in ("IB_HOST", "IBKR_HOST", "IB_PORT", "IBKR_PORT", "IB_CLIENT_ID", "IBKR_ALLOW_LIVE",
              "IBKR_OPS_ALERT_CMD", "TENDER_BOT_CONFIG", "TENDER_BOT_STATE"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cfg(tmp_path):
    from tender_bot import config

    c = copy.deepcopy(config.DEFAULTS)
    c["state_dir"] = str(tmp_path / "state")
    c["external_holdings"] = {}
    c["rest_seconds"] = 0
    return c
