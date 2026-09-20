import os
import json
import math
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lambda"))
from renko import _wilder_atr_series  # noqa: E402

DATA_DIR = os.path.dirname(os.path.abspath(__file__))

with open(f"{DATA_DIR}/candles_1h.json") as f:
    candles_1h_raw = json.load(f)
with open(f"{DATA_DIR}/funding_1h.json") as f:
    funding_raw = json.load(f)
with open(f"{DATA_DIR}/dvol_1h.json") as f:
    dvol_raw = json.load(f)

candles_1h = sorted(((int(ts), v) for ts, v in candles_1h_raw.items()), key=lambda x: x[0])
funding_1h = {int(ts): v for ts, v in funding_raw.items()}
dvol_1h = sorted(((int(ts), v) for ts, v in dvol_raw.items()), key=lambda x: x[0])

H4_MS = 4 * 60 * 60 * 1000
ATR_PERIOD = 14
ATR_MULT = 0.15
TICK_TREND = 2
TICK_REVERSAL = 4
SIZE = 2.0  # ETH
EXPIRY_DAYS = 30
STRIKE_ROUND = 25  # round ATM strike to nearest $25, approximating Deribit's listed strike grid
OPTION_FEE_RATE = 0.0003  # ~0.03% of underlying notional, Deribit's typical options taker fee
PERP_FEE_RATE = 0.0005  # ~0.05% taker fee per side, conservative

# --- Build H4 candles from 1H ---
buckets = {}
for ts, v in candles_1h:
    b = ts - (ts % H4_MS)
    row = buckets.setdefault(b, {"high": v["high"], "low": v["low"], "close": v["close"]})
    row["high"] = max(row["high"], v["high"])
    row["low"] = min(row["low"], v["low"])
    row["close"] = v["close"]
h4_keys = sorted(buckets.keys())
h4_candles = [buckets[k] for k in h4_keys]
print(f"{len(h4_candles)} H4 candles from {datetime.utcfromtimestamp(h4_keys[0]/1000)} to {datetime.utcfromtimestamp(h4_keys[-1]/1000)}")

# --- DVOL: nearest-hour lookup ---
dvol_ts = [d[0] for d in dvol_1h]
dvol_val = [d[1] for d in dvol_1h]


def dvol_at(ts):
    import bisect
    i = bisect.bisect_left(dvol_ts, ts)
    if i == 0:
        return dvol_val[0]
    if i >= len(dvol_ts):
        return dvol_val[-1]
    before, after = dvol_ts[i - 1], dvol_ts[i]
    return dvol_val[i - 1] if (ts - before) <= (after - ts) else dvol_val[i]


# --- Black-Scholes (r=0, no dividend) ---
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T_years, sigma, is_call):
    if T_years <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0) if is_call else max(K - S, 0)
        return intrinsic
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * math.sqrt(T_years))
    d2 = d1 - sigma * math.sqrt(T_years)
    if is_call:
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


# --- Renko direction series (streaming, matches renko.compute_trend bar-by-bar) ---
atrs = _wilder_atr_series(h4_candles, ATR_PERIOD)
start_idx = min(atrs.keys()) if atrs else None

directions = [None] * len(h4_candles)  # direction AFTER processing candle i
bar_close = h4_candles[start_idx]["close"]
tick0 = atrs[start_idx] * ATR_MULT
bar_max = bar_close + TICK_TREND * tick0
bar_min = bar_close - TICK_TREND * tick0
direction = 0

for t in range(start_idx, len(h4_candles)):
    if t > start_idx:
        if t not in atrs:
            directions[t] = direction if direction != 0 else None
            continue
        price = h4_candles[t]["close"]
        tick = atrs[t] * ATR_MULT
        if price > bar_max:
            direction = 1
            this_close = bar_max
            bar_max = this_close + TICK_TREND * tick
            bar_min = this_close - TICK_REVERSAL * tick
        elif price < bar_min:
            direction = -1
            this_close = bar_min
            bar_max = this_close + TICK_REVERSAL * tick
            bar_min = this_close - TICK_TREND * tick
    directions[t] = direction if direction != 0 else None

print("First non-None direction at index", next(i for i, d in enumerate(directions) if d is not None))

# --- Simulate perp hedge (flips on direction change) ---
perp_events = []  # (timestamp, action, price, pnl)
perp_position = 0  # -SIZE, 0, +SIZE
perp_entry_price = None
perp_realized = 0.0
perp_fees = 0.0
funding_pnl = 0.0
prev_dir = None

# index funding by H4 bucket start for fast lookup of hours within it
funding_hours_sorted = sorted(funding_1h.keys())


def funding_sum_for_bucket(bucket_start):
    total = 0.0
    for h in range(4):
        ts = bucket_start + h * 3600 * 1000
        if ts in funding_1h:
            total += funding_1h[ts]
    return total


equity_curve = []  # (timestamp, cumulative_pnl)
cum_pnl = 0.0

for t in range(start_idx, len(h4_candles)):
    ts = h4_keys[t]
    d = directions[t]
    price = h4_candles[t]["close"]

    # funding accrues on whatever position was open during this bucket
    if perp_position != 0:
        rate_sum = funding_sum_for_bucket(ts)
        notional = perp_position * price
        # Deribit: longs pay shorts when funding positive. Position holder receives -rate*notional.
        funding_pnl += -rate_sum * notional
        cum_pnl += -rate_sum * notional

    if d is not None and d != prev_dir:
        new_position = SIZE if d == 1 else -SIZE
        if perp_position != 0:
            leg_pnl = (price - perp_entry_price) * perp_position
            fee = abs(perp_position) * price * PERP_FEE_RATE
            perp_realized += leg_pnl
            perp_fees += fee
            cum_pnl += leg_pnl - fee
            perp_events.append((ts, "close", price, leg_pnl - fee))
        # open new leg
        fee = abs(new_position) * price * PERP_FEE_RATE
        perp_fees += fee
        cum_pnl -= fee
        perp_position = new_position
        perp_entry_price = price
        perp_events.append((ts, "open_" + ("long" if d == 1 else "short"), price, -fee))
        prev_dir = d

    equity_curve.append((ts, cum_pnl))

# close final open perp leg at last price (mark-to-market)
if perp_position != 0:
    last_price = h4_candles[-1]["close"]
    leg_pnl = (last_price - perp_entry_price) * perp_position
    perp_realized += leg_pnl
    cum_pnl += leg_pnl
    equity_curve.append((h4_keys[-1], cum_pnl))

print(f"Perp: {len([e for e in perp_events if e[1].startswith('open')])} flips, "
      f"realized+mtm={perp_realized:.2f}, fees={perp_fees:.2f}, funding={funding_pnl:.2f}")

# --- Simulate rolling short ATM straddle (put+call, 30D, monthly roll) ---
option_cycles = []
cum_options_pnl = 0.0
cursor_idx = start_idx
h4_close_price = [c["close"] for c in h4_candles]

while cursor_idx < len(h4_candles):
    entry_ts = h4_keys[cursor_idx]
    S0 = h4_close_price[cursor_idx]
    K = round(S0 / STRIKE_ROUND) * STRIKE_ROUND
    sigma = dvol_at(entry_ts) / 100.0
    T = EXPIRY_DAYS / 365.0

    call_premium = bs_price(S0, K, T, sigma, True)
    put_premium = bs_price(S0, K, T, sigma, False)
    entry_fee = (call_premium + put_premium) * SIZE * OPTION_FEE_RATE * 0  # fee already ~ capped small; approximate via flat bps below
    entry_notional_fee = 2 * S0 * SIZE * OPTION_FEE_RATE  # flat approx: 0.03% of underlying per contract, both legs, both sides(open)

    expiry_target = entry_ts + EXPIRY_DAYS * 24 * 3600 * 1000
    # find first H4 candle at/after expiry_target
    expiry_idx = None
    for j in range(cursor_idx, len(h4_candles)):
        if h4_keys[j] >= expiry_target:
            expiry_idx = j
            break
    if expiry_idx is None:
        # not enough data left for a full cycle; mark-to-market at last available bar and stop
        S_now = h4_close_price[-1]
        remaining_T = max((h4_keys[-1] - entry_ts) / (365 * 24 * 3600 * 1000), 1e-6)
        call_now = bs_price(S_now, K, max(T - remaining_T, 1e-6), dvol_at(h4_keys[-1]) / 100.0, True)
        put_now = bs_price(S_now, K, max(T - remaining_T, 1e-6), dvol_at(h4_keys[-1]) / 100.0, False)
        cycle_pnl = (call_premium + put_premium - call_now - put_now) * SIZE - entry_notional_fee
        cum_options_pnl += cycle_pnl
        option_cycles.append({
            "entry": entry_ts, "expiry": h4_keys[-1], "S0": S0, "K": K, "sigma": sigma,
            "premium": (call_premium + put_premium) * SIZE, "payout": (call_now + put_now) * SIZE,
            "pnl": cycle_pnl, "final_partial": True,
        })
        break

    S_exp = h4_close_price[expiry_idx]
    call_payout = max(S_exp - K, 0)
    put_payout = max(K - S_exp, 0)
    cycle_pnl = (call_premium + put_premium - call_payout - put_payout) * SIZE - entry_notional_fee
    cum_options_pnl += cycle_pnl
    option_cycles.append({
        "entry": entry_ts, "expiry": h4_keys[expiry_idx], "S0": S0, "K": K, "sigma": sigma,
        "premium": (call_premium + put_premium) * SIZE, "payout": (call_payout + put_payout) * SIZE,
        "S_exp": S_exp, "pnl": cycle_pnl, "final_partial": False,
    })
    cursor_idx = expiry_idx

print(f"Options: {len(option_cycles)} cycles, total pnl={cum_options_pnl:.2f}")

total_pnl = cum_pnl + cum_options_pnl
print(f"\n=== TOTAL 3Y PNL: {total_pnl:.2f} USD (perp {cum_pnl:.2f} + options {cum_options_pnl:.2f}) ===")

with open(f"{DATA_DIR}/backtest_results.json", "w") as f:
    json.dump({
        "h4_keys": h4_keys,
        "directions": directions,
        "equity_curve": equity_curve,
        "perp_events": perp_events,
        "option_cycles": option_cycles,
        "perp_realized": perp_realized,
        "perp_fees": perp_fees,
        "funding_pnl": funding_pnl,
        "options_pnl": cum_options_pnl,
        "total_pnl": total_pnl,
        "h4_close_price": h4_close_price,
    }, f)
print("Saved results.")
