import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hedge_designs as hd  # noqa: E402
from hedge_designs import simulate  # noqa: E402

H4 = 4 * 3600000
BARS_PER_MONTH = 6 * 30

cands = [
    ("A current Renko fixed",   "renko", dict()),
    ("G 30D delta IV>=60",      "delta", dict(band=0.3, iv_min=60)),
    ("I' 30D tilt0.5 IV>=60",   "tilt",  dict(band=0.3, tilt=0.5, iv_min=60)),
    ("N' 7D tilt0.5 IV>=60",    "tilt",  dict(band=0.3, tilt=0.5, tenor_days=7, iv_min=60)),
]

print("=== 1. Start-month robustness (start 0/3/6/9/12 months in; P&L shown per year of run) ===")
print(f"{'config':<26}{'mean/yr':>9}{'min/yr':>9}{'max/yr':>9}{'mean DD':>9}{'worst DD':>9}{'ret/DD':>8}")
for name, hedge, kw in cands:
    per_yr, dds = [], []
    for m in (0, 3, 6, 9, 12):
        w = 20 + m * BARS_PER_MONTH
        r, mp = simulate(name, H4, hedge, warmup_bars=w, **kw)
        years = len(mp) / 12
        per_yr.append(r["total"] / years); dds.append(r["mdd"])
    n = len(per_yr); mean_y = sum(per_yr) / n; mean_dd = sum(dds) / n
    print(f"{name:<26}{mean_y:>9.0f}{min(per_yr):>9.0f}{max(per_yr):>9.0f}{mean_dd:>9.0f}{min(dds):>9}{mean_y / abs(mean_dd):>8.2f}")

print("\n=== 2. Split-sample: first half (Sep23-Mar25) vs second half (Mar25-Sep26), P&L per year ===")
total_bars = len(hd.build_bars(H4)[0])
half = total_bars // 2
print(f"{'config':<26}{'H1 /yr':>9}{'H1 DD':>8}{'H2 /yr':>9}{'H2 DD':>8}")
for name, hedge, kw in cands:
    # first half: truncate data by monkeypatching c1h
    full = hd.c1h
    keys_all = hd.build_bars(H4)[0]
    cut_ts = keys_all[half]
    hd.c1h = [x for x in full if x[0] < cut_ts]
    r1, mp1 = simulate(name, H4, hedge, **kw)
    hd.c1h = full
    r2, mp2 = simulate(name, H4, hedge, warmup_bars=half, **kw)
    y1 = len(mp1) / 12; y2 = len(mp2) / 12
    print(f"{name:<26}{r1['total'] / y1:>9.0f}{r1['mdd']:>8}{r2['total'] / y2:>9.0f}{r2['mdd']:>8}")

print("\n=== 3. Perp fee sensitivity (taker 0.05% vs maker-ish 0.01%) ===")
print(f"{'config':<26}{'total@.05%':>11}{'DD':>7}{'total@.01%':>11}{'DD':>7}")
for name, hedge, kw in cands:
    hd.PERP_FEE = 0.0005; r_t, _ = simulate(name, H4, hedge, **kw)
    hd.PERP_FEE = 0.0001; r_m, _ = simulate(name, H4, hedge, **kw)
    hd.PERP_FEE = 0.0005
    print(f"{name:<26}{r_t['total']:>11}{r_t['mdd']:>7}{r_m['total']:>11}{r_m['mdd']:>7}")
