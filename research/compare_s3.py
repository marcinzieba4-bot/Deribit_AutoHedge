"""Reconciliation against the earlier "ImpliedVol Premia" study (repo
marcinzieba4-bot/Straddle_With_Hedge, branch claude/youthful-hopper-weo14c,
backtest_v3.py). Everything is expressed in that study's units: month P&L as
a percent of spot at month start; Sharpe = mean/sd*sqrt(12); drawdown on the
additive cumulative return; Calmar = 12*mean/|max drawdown|.

Prints: this repo's books in those units, the earlier study's own result
files, the earlier rule (daily Renko, one-unit hedge, break-even flip,
next-open fills, 90% of mark) re-run on this repo's dataset at daily and
4-hour granularity, the 90%-of-mark sensitivity, and BTC / 50-50 variants.

Needs candles_1h.json, funding_1h.json, dvol_1h.json (fetch_data.py), the
btc_* equivalents (fetch_data_btc.py) and a checkout of the earlier repo at
S3_REPO for its results_eth_v3.csv.
"""
import csv
import json
import math
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hedge_designs as hd  # noqa: E402

S3_REPO = os.environ.get("S3_REPO", "/home/user/marcinzieba4-bot/straddle_with_hedge")
H4 = 4 * 3600000
DATA_DIR = os.path.dirname(os.path.abspath(__file__))


def stats(rets):
    r = list(rets)
    n = len(r)
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1))
    cum = peak = mdd = 0.0
    for x in r:
        cum += x
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return dict(n=n, avg=round(mean, 2), sharpe=round(mean / sd * math.sqrt(12), 2), mdd=round(mdd, 1),
                calmar=round(mean * 12 / abs(mdd), 2) if mdd else None, worst=round(min(r), 1),
                pos=f"{sum(1 for x in r if x > 0)}/{n}")


def spot_map():
    keys, bars = hd.build_bars(H4)
    s = {}
    for k, b in zip(keys, bars):
        s.setdefault(datetime.utcfromtimestamp(k / 1000).strftime("%Y-%m"), b["close"])
    return s


def pct_series(mp, spot0, lo="2023-09", hi="2026-12"):
    return {f"{y}-{m:02d}": v / spot0[f"{y}-{m:02d}"] * 100
            for (y, m), v in mp if lo <= f"{y}-{m:02d}" <= hi and f"{y}-{m:02d}" in spot0}


def window(series, lo="2023-10", hi="2026-06"):
    return [v for k, v in sorted(series.items()) if lo <= k <= hi]


CONFIGS = {
    "pure short straddle": ("none", dict(size=1.0)),
    "A fixed 1 ETH Renko flip (H4)": ("renko", dict(size=1.0)),
    "vol sleeve (delta + 0.5 lean, DVOL>=60)": ("tilt", dict(band=0.3, tilt=0.5, iv_min=60, size=1.0)),
    "vol + trend sleeve 0.5": ("tilt_mom_add", dict(band=0.3, tilt=0.5, iv_min=60, size=1.0, mom_size=0.5)),
    "vol + trend sleeve 1.0": ("tilt_mom_add", dict(band=0.3, tilt=0.5, iv_min=60, size=1.0, mom_size=1.0)),
    "trend sleeve alone": ("renko_always", dict(options="none", iv_max=60, size=1.0)),
}


def v3_rule(bars, mult):
    """The earlier study's 'entry' variant on OHLC bars (daily or H4)."""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    opens = [b["open"] for b in bars]
    n = len(closes)
    tr = [None] * n
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = [None] * n
    atr[14] = sum(tr[1:15]) / 14
    for i in range(15, n):
        atr[i] = (atr[i - 1] * 13 + tr[i]) / 14
    sig = [None] * n
    tick = atr[14] * mult
    bmax, bmin, d = closes[14] + 2 * tick, closes[14] - 2 * tick, 0
    for i in range(15, n):
        p = closes[i]
        if p > bmax or p < bmin:
            d = 1 if p > bmax else -1
            tc = bmax if d == 1 else bmin
            tick = atr[i] * mult
            bmax, bmin = (tc + 2 * tick, tc - 4 * tick) if d > 0 else (tc + 4 * tick, tc - 2 * tick)
        sig[i] = d if d else None
    groups = {}
    for i, b in enumerate(bars):
        groups.setdefault(datetime.utcfromtimestamp(b["ts"] / 1000).strftime("%Y-%m"), []).append(i)
    out = {}
    for ym, idxs in sorted(groups.items()):
        idxs = [i for i in idxs if sig[i] is not None]
        if len(idxs) < 2:
            continue
        i0 = idxs[0]
        S0 = closes[i0]
        K = round(S0 / 50) * 50
        days = max((bars[idxs[-1]]["ts"] - bars[i0]["ts"]) / 86400000, 1)
        T = days / 365
        sigma = hd.dvol_at(bars[i0]["ts"]) / 100
        prem = 2 * 0.9 * (1 / math.sqrt(2 * math.pi)) * sigma * math.sqrt(T) * S0 + max(0, S0 - K) + max(0, K - S0)
        fees = 2 * 3e-4 * S0
        hd_dir = sig[i0]
        entry = opens[i0 + 1] if i0 + 1 < n else S0
        hp = 0.0
        fees += 5e-4 * entry
        for i in idxs[1:]:
            p, s = closes[i], sig[i]
            if s is not None and s != hd_dir:
                itm = (hd_dir == 1 and p > entry) or (hd_dir == -1 and p < entry)
                if not itm and i + 1 < n and i < idxs[-1]:
                    fill = opens[i + 1]
                    hp += hd_dir * (fill - entry)
                    hd_dir, entry = s, fill
                    fees += 2 * 5e-4 * fill
        settle = closes[idxs[-1]]
        hp += hd_dir * (settle - entry)
        fees += 5e-4 * settle + 0.5e-4 * S0 * days
        opt = prem - max(0, settle - K) - max(0, K - settle)
        out[ym] = (opt + hp - fees) / S0 * 100
    return out


def daily_bars():
    d = {}
    for ts, v in hd.c1h:
        k = ts - ts % 86400000
        r = d.setdefault(k, {"high": v["high"], "low": v["low"], "close": v["close"], "ts": k})
        r["high"] = max(r["high"], v["high"])
        r["low"] = min(r["low"], v["low"])
        r["close"] = v["close"]
    out = [d[k] for k in sorted(d)]
    for i in range(len(out)):
        out[i]["open"] = out[i - 1]["close"] if i else out[i]["close"]
    return out


def h4_bars():
    keys, bars = hd.build_bars(H4)
    return [{"open": bars[i - 1]["close"] if i else bars[i]["close"], "high": bars[i]["high"],
             "low": bars[i]["low"], "close": bars[i]["close"], "ts": keys[i]} for i in range(len(bars))]


def load_btc():
    hd.c1h = sorted(((int(k), v) for k, v in json.load(open(f"{DATA_DIR}/btc_candles_1h.json")).items()))
    hd.funding = {int(k): v for k, v in json.load(open(f"{DATA_DIR}/btc_funding_1h.json")).items()}
    hd.dvol = sorted(((int(k), v) for k, v in json.load(open(f"{DATA_DIR}/btc_dvol_1h.json")).items()))
    hd.dv_ts = [d[0] for d in hd.dvol]
    hd.dv_v = [d[1] for d in hd.dvol]
    hd.STRIKE_STEP = 500


if __name__ == "__main__":
    spot = spot_map()
    eth = {}
    print("=== this repo's ETH books, % of spot: full Sep23-Sep26 | common Oct23-Jun26 ===")
    for name, (hedge, kw) in CONFIGS.items():
        _, mp = hd.simulate(name, H4, hedge, **kw)
        eth[name] = pct_series(mp, spot)
        print(f"{name:<42} full {stats(eth[name].values())}\n{'':<42} comm {stats(window(eth[name]))}")

    csv_path = os.path.join(S3_REPO, "results_eth_v3.csv")
    if os.path.exists(csv_path):
        their = {r["month"]: float(r["ret_pct"]) for r in csv.DictReader(open(csv_path))}
        print("\n=== earlier study, ETH result file ===")
        print("full", stats(their.values()), "\ncomm", stats(window(their)))

    print("\n=== earlier rule re-run on this dataset ===")
    for mult in (0.15, 0.075):
        s = v3_rule(daily_bars(), mult)
        print(f"daily x{mult}: full {stats(s.values())} | comm {stats(window(s))}")
    s = v3_rule(h4_bars(), 0.15)
    print(f"H4 x0.15:     full {stats(s.values())} | comm {stats(window(s))}")

    print("\n=== 90% of mark sensitivity (common window) ===")
    for frac in (1.0, 0.9):
        hd.MARK_FRAC = frac
        for name in ("vol sleeve (delta + 0.5 lean, DVOL>=60)", "vol + trend sleeve 0.5"):
            hedge, kw = CONFIGS[name]
            _, mp = hd.simulate(name, H4, hedge, **kw)
            print(f"mark {int(frac * 100)}% {name:<42} {stats(window(pct_series(mp, spot)))}")
    hd.MARK_FRAC = 1.0

    if os.path.exists(f"{DATA_DIR}/btc_candles_1h.json"):
        load_btc()
        bspot = spot_map()
        print(f"\n=== BTC (same parameters; DVOL>=60 share {100 * sum(1 for v in hd.dv_v if v >= 60) / len(hd.dv_v):.0f}%) and 50/50 ===")
        for name in ("vol sleeve (delta + 0.5 lean, DVOL>=60)", "vol + trend sleeve 0.5", "trend sleeve alone"):
            hedge, kw = CONFIGS[name]
            _, mp = hd.simulate(name, H4, hedge, **kw)
            b = pct_series(mp, bspot)
            ks = sorted(set(b) & set(eth[name]))
            print(f"BTC   {name:<42} {stats(b.values())}")
            print(f"50/50 {name:<42} {stats([(eth[name][k] + b[k]) / 2 for k in ks])}")
