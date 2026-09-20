import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hedge_designs import simulate  # noqa: E402

H4 = 4 * 3600000
BPM = 180  # H4 bars per month


def row(r):
    return (f"{r['name']:<46}{r['total']:>7}{r['mdd']:>7}{str(r['ret_dd']):>7}{r['opt']:>7}{r['hedge']:>7}"
            f"{r['fees']:>6}{r['trades']:>7}{r['cycles']:>4}{r['pos_m']:>7}  {r['worst_m']}")


hdr = f"{'config (all: enter only when DVOL < 60)':<46}{'total':>7}{'maxDD':>7}{'ret/DD':>7}{'opt':>7}{'hedge':>7}{'fees':>6}{'trades':>7}{'cyc':>4}{'pos mo':>7}  worst month"

cands = [
    ("L1 long 30D straddle, delta hedge b0.3",       "delta",        dict(options="long", band=0.3, iv_max=60)),
    ("L2 long 30D straddle, unhedged",               "none",         dict(options="long", iv_max=60)),
    ("L3 long 30D straddle + tilt 0.5",              "tilt",         dict(options="long", band=0.3, tilt=0.5, iv_max=60)),
    ("L4 long 30D straddle + tilt 1.0",              "tilt",         dict(options="long", band=0.3, tilt=1.0, iv_max=60)),
    ("L5 long 30D straddle + Renko full overlay",    "renko",        dict(options="long", iv_max=60)),
    ("L6 long 7D straddle, delta hedge b0.3",        "delta",        dict(options="long", band=0.3, tenor_days=7, iv_max=60)),
    ("L7 long 7D straddle + tilt 0.5",               "tilt",         dict(options="long", band=0.3, tilt=0.5, tenor_days=7, iv_max=60)),
    ("L8 Renko momentum on perp only, 2 ETH",        "renko_always", dict(options="none", iv_max=60)),
    ("L9 Renko momentum on perp only, 1 ETH",        "renko_always", dict(options="none", iv_max=60, size=1.0)),
    ("L10 SHORT 30D straddle (for reference)",       "delta",        dict(options="short", band=0.3, iv_max=60)),
]

print("=== 1. Low-vol regime candidates (3y, 2 ETH unless noted) ===")
print(hdr)
results = {}
for name, hedge, kw in cands:
    r, _ = simulate(name, H4, hedge, **kw)
    results[name] = r
    print(row(r))

print("\n=== 2. DVOL ceiling sensitivity (long 30D, delta b0.3 / +tilt 0.5 / 7D delta) ===")
print(hdr)
for cap in (50, 55, 60, 65):
    for label, hedge, kw in (("delta b0.3", "delta", dict(band=0.3)), ("tilt 0.5", "tilt", dict(band=0.3, tilt=0.5)),
                             ("7D delta b0.3", "delta", dict(band=0.3, tenor_days=7))):
        r, _ = simulate(f"long 30D {label}, DVOL<{cap}", H4, hedge, options="long", iv_max=cap, **kw)
        print(row(r))

print("\n=== 3. Start-date robustness (0/3/6/9/12 months in), P&L per year of run ===")
print(f"{'config':<46}{'mean/yr':>9}{'min/yr':>9}{'max/yr':>9}{'mean DD':>9}{'worst DD':>9}")
for name, hedge, kw in cands:
    per_yr, dds = [], []
    for m in (0, 3, 6, 9, 12):
        r, mp = simulate(name, H4, hedge, warmup_bars=20 + m * BPM, **kw)
        per_yr.append(r["total"] / (len(mp) / 12)); dds.append(r["mdd"])
    n = len(per_yr)
    print(f"{name:<46}{sum(per_yr)/n:>9.0f}{min(per_yr):>9.0f}{max(per_yr):>9.0f}{sum(dds)/n:>9.0f}{min(dds):>9}")

print("\n=== 4. Full-time system: live I' (short, DVOL>=60) + each low-vol book, summed ===")
base, _ = simulate("I'", H4, "tilt", band=0.3, tilt=0.5, iv_min=60)
base_eq = dict(base["equity"])
print(f"{'I'' alone':<46}{base['total']:>7}{base['mdd']:>7}{str(base['ret_dd']):>7}")
print(f"{'combined with:':<46}{'total':>7}{'maxDD':>7}{'ret/DD':>7}{'low-vol book':>13}")
for name, hedge, kw in cands:
    r = results[name]
    if name.startswith("L10"):
        continue
    eq = [(ts, e + base_eq.get(ts, 0.0)) for ts, e in r["equity"]]
    peak = -1e18; mdd = 0.0
    for _, e in eq:
        peak = max(peak, e); mdd = min(mdd, e - peak)
    tot = eq[-1][1]
    print(f"{'  + ' + name:<46}{tot:>7.0f}{mdd:>7.0f}{tot/abs(mdd) if mdd else 0:>7.2f}{r['total']:>13}")
