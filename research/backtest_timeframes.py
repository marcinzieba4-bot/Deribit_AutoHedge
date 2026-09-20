import os
import json
import math
import sys
from datetime import datetime

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

ATR_PERIOD = 14
ATR_MULT = 0.15
TICK_TREND = 2
TICK_REVERSAL = 4
SIZE = 2.0
EXPIRY_DAYS = 30
STRIKE_ROUND = 25
OPTION_FEE_RATE = 0.0003
PERP_FEE_RATE = 0.0005

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


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T_years, sigma, is_call):
    if T_years <= 0 or sigma <= 0:
        return max(S - K, 0) if is_call else max(K - S, 0)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * math.sqrt(T_years))
    d2 = d1 - sigma * math.sqrt(T_years)
    if is_call:
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def run_backtest(bucket_ms, label):
    buckets = {}
    for ts, v in candles_1h:
        b = ts - (ts % bucket_ms)
        row = buckets.setdefault(b, {"high": v["high"], "low": v["low"], "close": v["close"]})
        row["high"] = max(row["high"], v["high"])
        row["low"] = min(row["low"], v["low"])
        row["close"] = v["close"]
    keys = sorted(buckets.keys())
    candles = [buckets[k] for k in keys]

    atrs = _wilder_atr_series(candles, ATR_PERIOD)
    start_idx = min(atrs.keys())

    directions = [None] * len(candles)
    bar_close = candles[start_idx]["close"]
    tick0 = atrs[start_idx] * ATR_MULT
    bar_max = bar_close + TICK_TREND * tick0
    bar_min = bar_close - TICK_TREND * tick0
    direction = 0
    for t in range(start_idx, len(candles)):
        if t > start_idx:
            if t not in atrs:
                directions[t] = direction if direction != 0 else None
                continue
            price = candles[t]["close"]
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

    def funding_sum_for_bucket(bucket_start):
        total = 0.0
        hours = bucket_ms // 3600000
        for h in range(hours):
            ts = bucket_start + h * 3600 * 1000
            if ts in funding_1h:
                total += funding_1h[ts]
        return total

    perp_position = 0
    perp_entry_price = None
    perp_realized = 0.0
    perp_fees = 0.0
    funding_pnl = 0.0
    prev_dir = None
    flips = 0
    equity_curve = []
    cum_pnl = 0.0

    for t in range(start_idx, len(candles)):
        ts = keys[t]
        d = directions[t]
        price = candles[t]["close"]
        if perp_position != 0:
            rate_sum = funding_sum_for_bucket(ts)
            notional = perp_position * price
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
                flips += 1
            fee = abs(new_position) * price * PERP_FEE_RATE
            perp_fees += fee
            cum_pnl -= fee
            perp_position = new_position
            perp_entry_price = price
            prev_dir = d
        equity_curve.append((ts, cum_pnl))

    if perp_position != 0:
        last_price = candles[-1]["close"]
        leg_pnl = (last_price - perp_entry_price) * perp_position
        perp_realized += leg_pnl
        cum_pnl += leg_pnl
        equity_curve.append((keys[-1], cum_pnl))

    # options leg (identical roll logic regardless of hedge timeframe)
    close_price = [c["close"] for c in candles]
    cursor_idx = start_idx
    cum_options_pnl = 0.0
    option_cycles = []
    while cursor_idx < len(candles):
        entry_ts = keys[cursor_idx]
        S0 = close_price[cursor_idx]
        K = round(S0 / STRIKE_ROUND) * STRIKE_ROUND
        sigma = dvol_at(entry_ts) / 100.0
        T = EXPIRY_DAYS / 365.0
        call_premium = bs_price(S0, K, T, sigma, True)
        put_premium = bs_price(S0, K, T, sigma, False)
        entry_notional_fee = 2 * S0 * SIZE * OPTION_FEE_RATE
        expiry_target = entry_ts + EXPIRY_DAYS * 24 * 3600 * 1000
        expiry_idx = None
        for j in range(cursor_idx, len(candles)):
            if keys[j] >= expiry_target:
                expiry_idx = j
                break
        if expiry_idx is None:
            S_now = close_price[-1]
            remaining_T = max((keys[-1] - entry_ts) / (365 * 24 * 3600 * 1000), 1e-6)
            call_now = bs_price(S_now, K, max(T - remaining_T, 1e-6), dvol_at(keys[-1]) / 100.0, True)
            put_now = bs_price(S_now, K, max(T - remaining_T, 1e-6), dvol_at(keys[-1]) / 100.0, False)
            cycle_pnl = (call_premium + put_premium - call_now - put_now) * SIZE - entry_notional_fee
            cum_options_pnl += cycle_pnl
            option_cycles.append({"entry": entry_ts, "expiry": keys[-1], "pnl": cycle_pnl})
            break
        S_exp = close_price[expiry_idx]
        call_payout = max(S_exp - K, 0)
        put_payout = max(K - S_exp, 0)
        cycle_pnl = (call_premium + put_premium - call_payout - put_payout) * SIZE - entry_notional_fee
        cum_options_pnl += cycle_pnl
        option_cycles.append({"entry": entry_ts, "expiry": keys[expiry_idx], "pnl": cycle_pnl})
        cursor_idx = expiry_idx

    total_pnl = cum_pnl + cum_options_pnl

    # max drawdown on combined (approximate: combine equity_curve perp with step options)
    opt_events = sorted([(c["expiry"], c["pnl"]) for c in option_cycles])
    opt_ts_list = [e[0] for e in opt_events]
    import bisect
    running = 0.0
    opt_cum_list = []
    for _, pnl in opt_events:
        running += pnl
        opt_cum_list.append(running)

    def opt_running_at(ts):
        i = bisect.bisect_right(opt_ts_list, ts) - 1
        return opt_cum_list[i] if i >= 0 else 0.0

    peak = -1e18
    max_dd = 0.0
    for ts, perp_val in equity_curve:
        combined = perp_val + opt_running_at(ts)
        peak = max(peak, combined)
        max_dd = min(max_dd, combined - peak)

    print(f"\n=== {label} ({bucket_ms//3600000}h bars, {len(candles)} bars) ===")
    print(f"Flips: {flips}, perp realized: {perp_realized:.2f}, perp fees: {perp_fees:.2f}, funding: {funding_pnl:.2f}")
    print(f"Options: {len(option_cycles)} cycles, pnl: {cum_options_pnl:.2f}")
    print(f"TOTAL: {total_pnl:.2f}  (perp {cum_pnl:.2f} + options {cum_options_pnl:.2f})")
    print(f"Max drawdown: {max_dd:.2f}")

    return {
        "label": label, "flips": flips, "perp_pnl": cum_pnl, "perp_realized": perp_realized,
        "perp_fees": perp_fees, "funding_pnl": funding_pnl, "options_pnl": cum_options_pnl,
        "total_pnl": total_pnl, "max_drawdown": max_dd, "n_cycles": len(option_cycles),
    }


h4_result = run_backtest(4 * 3600 * 1000, "H4")
d1_result = run_backtest(24 * 3600 * 1000, "D1")

with open(f"{DATA_DIR}/h4_vs_d1.json", "w") as f:
    json.dump({"h4": h4_result, "d1": d1_result}, f, indent=2)
