import os
import json
import math
import sys
import bisect
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lambda"))
from renko import _wilder_atr_series  # noqa: E402

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
c1h = sorted(((int(k), v) for k, v in json.load(open(f"{DATA_DIR}/candles_1h.json")).items()))
funding = {int(k): v for k, v in json.load(open(f"{DATA_DIR}/funding_1h.json")).items()}
dvol = sorted(((int(k), v) for k, v in json.load(open(f"{DATA_DIR}/dvol_1h.json")).items()))
dv_ts = [d[0] for d in dvol]
dv_v = [d[1] for d in dvol]

PERP_FEE = 0.0005
OPT_FEE = 0.0003
STRIKE_STEP = 25
MARK_FRAC = 1.0  # fraction of BS mark collected on short legs (0.9 = bid-side haircut)


def dvol_at(ts):
    i = bisect.bisect_left(dv_ts, ts)
    i = min(max(i, 0), len(dv_ts) - 1)
    return dv_v[i]


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, sig, call):
    if T <= 1e-9 or sig <= 0:
        return (max(S - K, 0), 1.0 if S > K else 0.0) if call else (max(K - S, 0), -1.0 if S < K else 0.0)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * ncdf(d1) - K * ncdf(d2), ncdf(d1)
    return K * ncdf(-d2) - S * ncdf(-d1), ncdf(d1) - 1


def build_bars(bucket_ms):
    b = {}
    for ts, v in c1h:
        k = ts - ts % bucket_ms
        r = b.setdefault(k, {"high": v["high"], "low": v["low"], "close": v["close"]})
        r["high"] = max(r["high"], v["high"]); r["low"] = min(r["low"], v["low"]); r["close"] = v["close"]
    keys = sorted(b)
    return keys, [b[k] for k in keys]


def renko_dirs(candles, period=14, mult=0.15, tt=2, tr=4):
    atrs = _wilder_atr_series(candles, period)
    s = min(atrs)
    out = [0] * len(candles)
    bc = candles[s]["close"]; tk = atrs[s] * mult
    bmax, bmin, d = bc + tt * tk, bc - tt * tk, 0
    for t in range(s + 1, len(candles)):
        if t in atrs:
            p = candles[t]["close"]; tk = atrs[t] * mult
            if p > bmax:
                d = 1; bmax, bmin = bmax + tt * tk, bmax - tr * tk
            elif p < bmin:
                d = -1; bmax, bmin = bmin + tr * tk, bmin - tt * tk
        out[t] = d
    return out


def simulate(name, bucket_ms, hedge, band=0.0, tilt=0.0, strangle=0.0, iv_min=0.0, size=2.0,
             tenor_days=30, warmup_bars=20, options="short", iv_max=1e9, mom_size=0.0):
    sgn = 1 if options == "short" else -1
    keys, bars = build_bars(bucket_ms)
    dirs = renko_dirs(bars) if hedge in ("renko", "tilt", "renko_always", "tilt_mom", "tilt_mom_add") else [0] * len(bars)
    hours = bucket_ms // 3600000

    pos = 0.0; hedge_pnl = 0.0; fees = 0.0; fund = 0.0; trades = 0
    opt_open = None; opt_realized = 0.0; premium_total = 0.0; cycles = 0; skipped_bars = 0
    equity = []; prev_S = None
    monthly = {}

    for t in range(warmup_bars, len(bars)):
        ts = keys[t]; S = bars[t]["close"]; sig = dvol_at(ts) / 100

        # hedge MTM + funding over the bar
        if prev_S is not None and pos != 0:
            hedge_pnl += pos * (S - prev_S)
            rs = sum(funding.get(ts + h * 3600000, 0.0) for h in range(hours))
            fund += -rs * pos * S
        prev_S = S

        # settle at expiry
        if opt_open and ts >= opt_open["exp"]:
            payout = max(S - opt_open["Kc"], 0) + max(opt_open["Kp"] - S, 0)
            opt_realized += sgn * (opt_open["prem"] - payout) * size
            opt_open = None; cycles += 1

        # open new straddle/strangle if flat and IV filter passes
        if opt_open is None and options != "none":
            if iv_min <= dvol_at(ts) < iv_max:
                Kc = round(S * (1 + strangle) / STRIKE_STEP) * STRIKE_STEP; Kp = round(S * (1 - strangle) / STRIKE_STEP) * STRIKE_STEP
                T = tenor_days / 365
                pc, _ = bs(S, Kc, T, sig, True); pp, _ = bs(S, Kp, T, sig, False)
                opt_open = {"Kc": Kc, "Kp": Kp, "exp": ts + tenor_days * 86400000, "prem": (pc + pp) * (MARK_FRAC if sgn == 1 else 2 - MARK_FRAC), "t0": ts}
                premium_total += (pc + pp) * size
                fees += 2 * S * size * OPT_FEE
            else:
                skipped_bars += 1

        # option MTM and delta
        if opt_open:
            T = max((opt_open["exp"] - ts) / (365 * 86400000), 1e-9)
            vc, dc = bs(S, opt_open["Kc"], T, sig, True); vp, dp = bs(S, opt_open["Kp"], T, sig, False)
            opt_mtm = sgn * (opt_open["prem"] - vc - vp) * size
            port_delta = -sgn * size * (dc + dp)
        else:
            opt_mtm = 0.0; port_delta = 0.0

        # hedge target
        if hedge == "none":
            target = 0.0
        elif hedge == "renko":
            target = size * dirs[t] if opt_open else 0.0
        elif hedge == "renko_always":
            target = size * dirs[t] if iv_min <= dvol_at(ts) < iv_max else 0.0
        elif hedge == "delta":
            target = -port_delta
        elif hedge == "tilt":
            target = -port_delta + tilt * dirs[t] if opt_open else 0.0
        elif hedge == "tilt_mom_add":  # deployed logic: momentum whenever DVOL < iv_min, added to the straddle hedge
            target = ((-port_delta + tilt * dirs[t]) if opt_open else 0.0) + (mom_size * dirs[t] if dvol_at(ts) < iv_min else 0.0)
        elif hedge == "tilt_mom":  # exact live logic: momentum only while no straddle is open
            target = (-port_delta + tilt * dirs[t]) if opt_open else (mom_size * dirs[t] if dvol_at(ts) < iv_min else 0.0)
        if abs(target - pos) > band or (target == 0 and pos != 0 and hedge not in ("renko",)):
            fees += abs(target - pos) * S * PERP_FEE; trades += 1; pos = target

        eq = hedge_pnl + fund - fees + opt_realized + opt_mtm
        equity.append((ts, eq))
        d = datetime.utcfromtimestamp(ts / 1000); monthly[(d.year, d.month)] = eq

    # metrics
    peak = -1e18; mdd = 0.0
    for _, e in equity:
        peak = max(peak, e); mdd = min(mdd, e - peak)
    total = equity[-1][1]
    mk = sorted(monthly); mp = []; prev = 0.0
    for k in mk:
        mp.append((k, monthly[k] - prev)); prev = monthly[k]
    worst = min(mp, key=lambda x: x[1]); pos_months = sum(1 for _, v in mp if v > 0)
    res = dict(name=name, total=round(total), mdd=round(mdd), ret_dd=round(total / abs(mdd), 2) if mdd else None,
               opt=round(opt_realized), hedge=round(hedge_pnl), fund=round(fund), fees=round(fees), trades=trades,
               cycles=cycles, prem=round(premium_total), worst_m=f"{worst[0][0]}-{worst[0][1]:02d} {worst[1]:+.0f}",
               pos_m=f"{pos_months}/{len(mp)}")
    res["equity"] = equity
    return res, mp


if __name__ == "__main__":
    configs = [
        ("A. current: Renko fixed 2 ETH, H4",          4,  "renko", dict()),
        ("B. no hedge (pure short straddle)",          4,  "none",  dict()),
        ("C. delta hedge, H4, band 0",                  4,  "delta", dict(band=0.0)),
        ("D. delta hedge, H4, band 0.3",                4,  "delta", dict(band=0.3)),
        ("E. delta hedge, H1, band 0.3",                1,  "delta", dict(band=0.3)),
        ("F. delta hedge, D1, band 0.3",                24, "delta", dict(band=0.3)),
        ("G. delta H4 b0.3 + IV>=60 filter",            4,  "delta", dict(band=0.3, iv_min=60)),
        ("H. Renko fixed H4 + IV>=60 filter",           4,  "renko", dict(iv_min=60)),
        ("I. delta+Renko tilt 0.5, H4 b0.3",            4,  "tilt",  dict(band=0.3, tilt=0.5)),
        ("J. 10% strangle, delta H4 b0.3",              4,  "delta", dict(band=0.3, strangle=0.10)),
        ("K. 10% strangle, delta H4 b0.3, IV>=60",      4,  "delta", dict(band=0.3, strangle=0.10, iv_min=60)),
        ("L. 10% strangle, Renko fixed H4, IV>=60",     4,  "renko", dict(strangle=0.10, iv_min=60)),
        ("M. delta H4 b0.3, 7D tenor",                  4,  "delta", dict(band=0.3, tenor_days=7)),
        ("N. delta H4 b0.3, 7D tenor, IV>=60",          4,  "delta", dict(band=0.3, tenor_days=7, iv_min=60)),
    ]

    results = []; monthlies = {}
    for name, hrs, hedge, kw in configs:
        r, mp = simulate(name, hrs * 3600000, hedge, **kw)
        results.append(r); monthlies[name] = mp

    hdr = f"{'config':<42}{'total':>8}{'maxDD':>8}{'ret/DD':>7}{'opt':>7}{'hedge':>7}{'fund':>6}{'fees':>6}{'trades':>7}{'cyc':>4}{'pos mo':>7}  worst month"
    print(hdr)
    for r in results:
        print(f"{r['name']:<42}{r['total']:>8}{r['mdd']:>8}{str(r['ret_dd']):>7}{r['opt']:>7}{r['hedge']:>7}{r['fund']:>6}{r['fees']:>6}{r['trades']:>7}{r['cycles']:>4}{r['pos_m']:>7}  {r['worst_m']}")

    json.dump({"results": results, "monthlies": {k: [[list(a), b] for a, b in v] for k, v in monthlies.items()}},
              open(f"{DATA_DIR}/hedge_designs.json", "w"))
