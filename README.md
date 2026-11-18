# btc-race-24h-bot

[![make.fun bounty](https://img.shields.io/badge/make.fun-bounty-ff2d78)](https://make.fun/bounty/btc-race-24h-20261118-1444)
[![Python 3.13](https://img.shields.io/badge/python-3.13-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A cbBTC 1-minute trading bot for the [make.fun](https://make.fun) bounty
[**Race a BTC trading bot live for 24 hours**](https://make.fun/bounty/btc-race-24h-20261118-1444)
($250 total, 10 places paid, 1st place $60).

- **Owner:** [muhammadainunnajib022-lgtm](https://github.com/muhammadainunnajib022-lgtm)
- **Repo:** [muhammadainunnajib022-lgtm/btc-race-24h-bot](https://github.com/muhammadainunnajib022-lgtm/btc-race-24h-bot)
- **Description:** cbBTC 1-minute trading bot for the make.fun bounty 'Race a BTC trading bot live for 24 hours' — buys deep corrections only. Python 3.13, standard library only.
- **Bounty:** https://make.fun/bounty/btc-race-24h-20261118-1444

## What it does

The bot trades Coinbase Wrapped BTC (cbBTC) against USDC on 1-minute bars
from its Orca Whirlpool on Solana, with $1,000 of paper money, under the
`agent-fun/bars-replay/v1` protocol: it reads one JSON line per minute
(`hello`, then one `batch` per bar, then `done`) and answers each batch with
exactly one `reply` carrying its buy/sell orders. Orders fill at the next
bar's open; a 4 bps fee, 2 bps of modelled slippage and constant-product
price impact apply. It is long only, holds at most one position, and uses
only the Python standard library — no network, no wall clock, no files
outside the repo, no randomness, so two runs on the same data give the
same orders.

## Strategy — buy deep corrections only

- Enter long, 100% of equity, only when the close is within 0.5% of the
  240-bar (4-hour) low, at least 1.5% below the 240-bar high, and RSI(14)
  is at or below 45.
- Exit on a 1.3% trailing stop, a 1.5% stop loss under average cost, or a
  360-bar time stop. No take-profit target: the trailing stop lets a real
  recovery run.
- 30-bar cooldown after exits; no new entries in the last 60 bars.
- Late-window fallback: if the bot has never traded by bar 1,080, the
  filters relax (0.8% / 0.8% / RSI 50) so a quiet window still yields the
  one trade the guardrails require.
- Risk limits: a 6% fall from peak equity flattens and halts trading; a
  3% fall from the start flattens and pauses for 240 bars. Every order
  names `max_slippage_bps` 150.

## Local result (dev window)

On the kit's frozen 24-hour dev window (1,440 one-minute bars, replayed
with the brief's fill model): **net PnL about +$18.90 (+1.89%), 4 fills
(2 round trips), maximum drawdown about 1.06%** — above the dev gate's
$0 and far inside the 25% drawdown guardrail. The house baselines on the
same window all lose (buy-and-hold -$31.63, momentum -$23.87,
RSI-Bollinger -$39.92, EMA-cross -$148.17, ADX-momentum -$75.57). No
dev-window result promises anything about the unseen race window.

## Run

```bash
python3 -P -s main.py
```

The grader runs exactly `python3 -P -s main.py` with `PYTHONHASHSEED=0`.
Diagnostics go to stderr only; stdout carries replies and nothing else.

To grade locally with the bounty kit:

```bash
node cli/makefun.mjs grade-local --kit . --bot <repository> --driver docker
```

## Repo layout

```text
main.py              <- the bot (Python 3.13, standard library only)
LICENSE              <- MIT
.makefun/owner.json  <- claim file naming the linked Solana wallet
```

## Licence

MIT — see [LICENSE](LICENSE).
