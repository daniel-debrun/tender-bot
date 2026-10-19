# tender-bot

Watches SEC EDGAR for issuer tender offers (Schedule TO-I) and, for offers whose terms give odd-lot holders
priority, buys up to 99 shares through Interactive Brokers so they can be tendered in full. It reminds you before
the tender deadline, sells when an offer is terminated or shares come back unaccepted, and keeps a P&L ledger.

Orders go through [ibkr-ops](https://github.com/daniel-debrun/ibkr-ops)'s gates: dry run by default, paper account
unless you explicitly allow live trading, caps per order and per day, a kill switch, and an append-only order ledger.

**Tendering is manual.** IBKR's API cannot make voluntary corporate-action elections, so the bot reminds you to
tender in IBKR and you confirm with `tender_bot confirm ID`. On a paper account the tender is simulated at the
payment date.

Personal tooling, not investment advice. Use at your own risk.

## Setup

Requires Python 3.10+, an IB Gateway (paper: port 4002) and ibkr-ops.

```bash
git clone https://github.com/daniel-debrun/tender-bot && cd tender-bot
uv venv -p 3.10 .venv
uv pip install -p .venv/bin/python ibkr-ops -e ".[test]"
.venv/bin/python -m pytest -q          # no Gateway or network needed
```

SEC requires a User-Agent naming you. Copy `config.example.json` to `~/.config/tender-bot/config.json` and set
`"user_agent": "Your Name you@example.com"` (or export `TENDER_BOT_USER_AGENT`). The bot refuses to scan without one.

## Commands

```
tender_bot scan [--evening]              EDGAR scan (the evening scan also re-checks recent daily indexes)
tender_bot trade [--entries] [--place]   exits; --entries also buys today's eligible offers (dry run without --place)
tender_bot deadline                      tender reminders and the pre-deadline price check
tender_bot summary                       daily summary
tender_bot status | show ID | ledger     inspect
tender_bot confirm ID | sell ID          after tendering in IBKR / to sell instead
tender_bot kill | unkill                 kill switch: blocks every order
tender_bot export [--out DIR]            offers / outcomes / fills / events as JSON
tender_bot heartbeats [--alert]          check that the scheduled jobs ran
tender_bot smoke [--symbol F] [--place]  paper order-path test (buy 1 share, sell it)
```

Live trading needs all three of: `"ibkr": {"allow_live": true}` in the config, `--live`, and `IBKR_ALLOW_LIVE=1`.

## Configuration

See `config.example.json` and the commented defaults in `tender_bot/config.py`. Main keys: `shares` (1-99),
`max_trade_value`, `max_open_positions`, `max_total_value`, `slippage`, `min_edge`, the eligibility switches
(`require_oddlot_provision`, `allow_any_and_all`, `exclude_oddlot_only`, `allowed_stock_types`), the exit switches
(`auto_sell_on_termination`, `auto_sell_remainder`, `auto_sell_above_offer`), `notify` and `ibkr`.
`$TENDER_BOT_CONFIG` and `$TENDER_BOT_STATE` override the config file and the state folder.

## Scheduling

`deploy/systemd/` has user units and timers (scans in market hours, an evening scan, the deadline check, morning
exits, entries near the close, a summary, heartbeats); `deploy/crontab.txt` is a cron alternative. Both assume the
checkout is at `~/tender-bot`; edit the paths otherwise.

```bash
cp deploy/systemd/tender-* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now tender-scan.timer
```

## Limits

- The parser is rule-based and can misread an offer; check the filing before tendering.
- Offers can be amended, extended or terminated, and not every offer gives odd-lot holders priority.
- Tender fees vary by broker and can exceed the gain on a small position.
- Another strategy trading the same account can sell the bot's shares before the tender; use a separate account.
