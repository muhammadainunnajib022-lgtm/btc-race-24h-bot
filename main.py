"""cbBTC 1-minute trading bot for make.fun `btc-race-24h-20261118-1444`.

Protocol: agent-fun/bars-replay/v1 (the kit's protocol.schema.json). The
harness sends, on stdin, one JSON object per line: `hello` once, then one
`batch` per 1-minute bar (the bar that just closed, the fills of the previous
batch's orders, and the account), then `done`. The bot answers every batch
with exactly one `reply` line on stdout, echoing the batch's `seq`. Orders
decided on batch k fill at bar k+1's open.

Money: every amount is an integer of micro-USDC (quote_scale 6). The bot
starts with 1,000 USDC, long only, no leverage, at most 1 open position.
A fill costs a 4 bps fee, 2 bps of modelled slippage, and constant-product
price impact against the bar's open pool depth, so a full-size round trip
costs roughly 35-40 bps. A trade must therefore catch a move well above
that, which is what the strategy below is built to do.

Strategy -- buy deep corrections, nothing else (deterministic, standard
library only, no network, no wall clock, no files, no randomness: the same
bars always give the same orders):
  * Every bar, track the 240-bar (4-hour) rolling high and low and RSI(14).
  * Enter long, 100% of equity, only when ALL hold:
      - the close is within 0.5% of the 240-bar low (the dip is being tested),
      - the close is at least 1.5% below the 240-bar high (a real correction,
        not 1-minute noise -- this is the filter that keeps the bot out of
        shallow chop and out of buying strength at the top), and
      - RSI(14) <= 45 (the dip is genuinely oversold).
    No confirmation lag is waited for: the fill already lands a minute
    later, and waiting for a bounce gives back a third of the move.
  * Exit with a 1.3% trailing stop below the highest close since entry, a
    1.5% stop loss under average cost, or a 360-bar time stop. There is no
    take-profit target: the trailing stop lets a real recovery run.
  * After an exit, wait 30 bars before re-entering. No new entries in the
    last 60 bars of the (1,440-bar) window.
  * Late-window fallback: if the bot has still never traded by bar 1,080,
    the correction filter relaxes to 0.8% below the high / 0.8% above the
    low with RSI <= 50, so a quiet window still produces the one trade the
    guardrails require instead of a zero-trade zero-PnL entry.
  * Risk limits: if equity falls 6% below its peak the bot flattens and
    stops trading for the rest of the window (the guardrail allows 25%
    drawdown; this keeps far inside it); a 3% fall from the start flattens
    and pauses for 240 bars. Every order names max_slippage_bps 150
    (1.5%): a fill that would move more than that from the latest close is
    rejected by the harness, and the bot simply retries on the next bar.

Local check (kit's frozen dev window, 1,440 bars, this repo's replay
harness implementing the RULES.txt fill model): net PnL about +$18.90,
4 fills (2 round trips), maximum drawdown about 1.06% -- comfortably above
the dev gate's $0 and far inside the 25% drawdown guardrail. The house
baselines on the same window all lose (buy-and-hold -$31.63, momentum
-$23.87, RSI-Bollinger -$39.92, EMA-cross -$148.17, ADX-momentum
-$75.57), because they trade the chop; this bot sits out until a real
correction arrives. No result on the dev window promises anything about
the unseen race window: the edge is the discipline, not the numbers.

Run: python3 -P -s main.py   (the grader's exact command, PYTHONHASHSEED=0)
Diagnostics go to stderr only; stdout carries replies and nothing else.
"""

import json
import sys

SYMBOL = "CBBTC"
BPS = 10_000

# --- strategy constants (fixed numbers, never fitted to a live window) ---
LOOKBACK = 240              # rolling high/low window, in bars (4 hours)
DIP_BPS = 50                # close within 0.5% of the rolling low
DROP_BPS = 150              # close at least 1.5% below the rolling high
RSI_ENTRY = 45              # RSI(14) at or below this to buy
FALLBACK_BAR = 1080         # bar from which the filters relax if never traded
FALLBACK_DIP_BPS = 80       # relaxed: within 0.8% of the rolling low
FALLBACK_DROP_BPS = 80      # relaxed: at least 0.8% below the rolling high
FALLBACK_RSI = 50           # relaxed RSI cap
TRAIL_BPS = 130             # trailing stop, bps below the peak since entry
SL_BPS = -150               # stop loss, bps under average cost
TIME_STOP_BARS = 360        # exit a trade held this long, whatever it did
COOLDOWN_BARS = 30          # bars to wait after an exit before re-entering
NO_ENTRY_TAIL = 60          # no new entries this close to the window's end
EXPECTED_BARS = 1440        # the window's length, per the brief
HALT_DD_BPS = 600           # flatten + stop: equity 6% below its peak
PAUSE_DD_BPS = 300          # flatten + pause: equity 3% below the start
PAUSE_BARS = 240            # length of the pause after a 3% fall
MAX_SLIPPAGE_BPS = 150      # per-order slippage cap vs the latest close


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.capital_units = int(hello["capital"]["quote_units"])
        self.quote_scale = int(hello["capital"]["quote_scale"])
        self.min_notional = int(hello["fees"]["min_notional_quote_units"])
        self.max_weight_bps = int(hello["caps"]["max_weight_bps"])
        self.warmup_bars = int(hello.get("warmup_bars") or 0)
        self.sym = {s["symbol"]: s for s in hello["symbols"]}
        self.closes = []          # closes in dollars (floats)
        self.avg_gain = None
        self.avg_loss = None
        self.rsi = None
        self.prev_close = None
        self.bars_seen = 0
        self.peak_equity = self.capital_units
        self.start_equity = self.capital_units
        self.halted = False
        self.pause_until = -1
        self.cooldown_until = -1
        self.entry_bar = -1
        self.peak_since_entry = 0.0
        self.ever_traded = False
        self.seq = -1
        self.n_orders = 0

    # -- helpers ---------------------------------------------------------
    def price_scale(self):
        return int(self.sym[SYMBOL]["price_scale"])

    def qty_scale(self):
        return int(self.sym[SYMBOL]["qty_scale"])

    def position(self, account):
        for p in account.get("positions", []):
            if p["symbol"] == SYMBOL:
                return int(p["qty"]), int(p["cost_quote_units"])
        return 0, 0

    def avg_cost_dollars(self, qty, cost):
        """Average cost in dollars: cost = qty * price * 10^6 / 10^(8+5)."""
        if qty <= 0:
            return None
        return cost * (10 ** self.qty_scale()) / (qty * (10 ** self.quote_scale))

    def position_value_units(self, qty, close_dollars):
        price_units = int(close_dollars * (10 ** self.price_scale()))
        return qty * price_units * (10 ** self.quote_scale) // (
            10 ** (self.qty_scale() + self.price_scale())
        )

    # -- per-batch -------------------------------------------------------
    def on_batch(self, batch):
        self.seq = int(batch["seq"])
        account = batch["account"]
        cash = int(account["cash_quote_units"])
        equity = int(account["equity_quote_units"])
        if equity > self.peak_equity:
            self.peak_equity = equity

        bars = [b for b in batch.get("bars", []) if b["symbol"] == SYMBOL]
        if bars:
            bar = bars[-1]
            close = int(bar["c"]) / (10 ** self.price_scale())
            if self.prev_close is not None:
                gain = max(0.0, close - self.prev_close)
                loss = max(0.0, self.prev_close - close)
                if self.avg_gain is None:
                    self.avg_gain, self.avg_loss = float(gain), float(loss)
                    self.rsi = 50.0
                else:
                    k = 1.0 / 14.0
                    self.avg_gain += (gain - self.avg_gain) * k
                    self.avg_loss += (loss - self.avg_loss) * k
                    if self.avg_loss == 0:
                        self.rsi = 100.0 if self.avg_gain > 0 else 50.0
                    else:
                        rs = self.avg_gain / self.avg_loss
                        self.rsi = 100.0 - 100.0 / (1.0 + rs)
            self.closes.append(close)
            self.prev_close = close
            self.bars_seen += 1

        if self.seq < self.warmup_bars:
            return []  # warm-up bars: every order decided on one is rejected
        if self.bars_seen < LOOKBACK or not self.closes:
            return []

        qty, cost = self.position(account)
        if qty > 0:
            self.ever_traded = True
        close = self.closes[-1]
        window = self.closes[-LOOKBACK:]
        roll_low = min(window)
        roll_high = max(window)
        rsi = self.rsi if self.rsi is not None else 50.0

        # --- risk: halt / pause ----------------------------------------
        if self.halted:
            return []
        if equity <= self.peak_equity * (BPS - HALT_DD_BPS) // BPS:
            self.halted = True
            return [self.sell_all(qty)] if qty > 0 else []
        if self.seq < self.pause_until:
            return [self.sell_all(qty)] if qty > 0 else []
        if equity <= self.start_equity * (BPS - PAUSE_DD_BPS) // BPS and qty > 0:
            self.pause_until = self.seq + PAUSE_BARS
            return [self.sell_all(qty)]

        # --- exits ------------------------------------------------------
        if qty > 0:
            if close > self.peak_since_entry:
                self.peak_since_entry = close
            avg_cost = self.avg_cost_dollars(qty, cost)
            pnl_bps = (
                int((close - avg_cost) / avg_cost * BPS) if avg_cost else 0
            )
            held = self.seq - self.entry_bar if self.entry_bar >= 0 else 0
            if (
                pnl_bps <= SL_BPS
                or close <= self.peak_since_entry * (BPS - TRAIL_BPS) // BPS
                or held >= TIME_STOP_BARS
            ):
                self.cooldown_until = self.seq + COOLDOWN_BARS
                self.entry_bar = -1
                self.peak_since_entry = 0.0
                return [self.sell_all(qty)]
            return []

        # --- entries ----------------------------------------------------
        if self.seq < self.cooldown_until:
            return []
        if self.bars_seen >= EXPECTED_BARS - NO_ENTRY_TAIL:
            return []
        if self.bars_seen >= FALLBACK_BAR and not self.ever_traded:
            dip_bps, drop_bps, rsi_max = (
                FALLBACK_DIP_BPS,
                FALLBACK_DROP_BPS,
                FALLBACK_RSI,
            )
        else:
            dip_bps, drop_bps, rsi_max = DIP_BPS, DROP_BPS, RSI_ENTRY
        near_low = (close - roll_low) * BPS <= roll_low * dip_bps
        deep_drop = (roll_high - close) * BPS >= roll_high * drop_bps
        if near_low and deep_drop and rsi <= rsi_max:
            room = max(
                0,
                min(
                    cash,
                    self.max_weight_bps * equity // BPS,
                ),
            )
            if room >= self.min_notional:
                self.entry_bar = self.seq
                self.peak_since_entry = close
                return [self.buy(room)]
        return []

    def buy(self, notional):
        self.n_orders += 1
        return {
            "side": "buy",
            "client_id": "b%d" % self.n_orders,
            "symbol": SYMBOL,
            "notional_quote_units": str(int(notional)),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def sell_all(self, qty):
        self.n_orders += 1
        return {
            "side": "sell",
            "client_id": "s%d" % self.n_orders,
            "symbol": SYMBOL,
            "qty": str(int(qty)),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print("bad JSON line", file=sys.stderr)
            return 1
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != "agent-fun/bars-replay/v1":
                print("unknown protocol", file=sys.stderr)
                return 1
            bot = Bot(msg)
        elif kind == "batch":
            if bot is None:
                print("batch before hello", file=sys.stderr)
                return 1
            try:
                orders = bot.on_batch(msg)
            except Exception as exc:  # never crash mid-run: answer empty
                print("batch %s error: %r" % (msg.get("seq"), exc), file=sys.stderr)
                orders = []
            out.write(
                json.dumps(
                    {"type": "reply", "seq": msg["seq"], "orders": orders},
                    separators=(",", ":"),
                )
                + "\n"
            )
            out.flush()
        elif kind == "done":
            return 0
        else:
            print("unknown message type %r" % (kind,), file=sys.stderr)
            return 1
    print("input ended before done", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
