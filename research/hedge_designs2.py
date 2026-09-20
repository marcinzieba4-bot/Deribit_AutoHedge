import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hedge_designs import simulate  # noqa: E402

H4 = 4 * 3600000


def row(r):
    return (f"{r['name']:<44}{r['total']:>8}{r['mdd']:>8}{str(r['ret_dd']):>7}{r['opt']:>7}{r['hedge']:>7}"
            f"{r['fees']:>6}{r['trades']:>7}{r['cycles']:>4}{r['pos_m']:>7}  {r['worst_m']}")


hdr = f"{'config':<44}{'total':>8}{'maxDD':>8}{'ret/DD':>7}{'opt':>7}{'hedge':>7}{'fees':>6}{'trades':>7}{'cyc':>4}{'pos mo':>7}  worst month"

print("=== 1. Tilt size x IV filter (30D tenor, H4, band 0.3) ===")
print(hdr)
for iv in (0, 60):
    for tilt in (0.25, 0.5, 1.0):
        r, _ = simulate(f"tilt {tilt}, IV>={iv}", H4, "tilt", band=0.3, tilt=tilt, iv_min=iv)
        print(row(r))

print("\n=== 2. 7D tenor: hedge type x IV threshold (H4, band 0.3) ===")
print(hdr)
for iv in (50, 60, 70):
    r, _ = simulate(f"7D delta, IV>={iv}", H4, "delta", band=0.3, tenor_days=7, iv_min=iv)
    print(row(r))
    r, _ = simulate(f"7D tilt 0.5, IV>={iv}", H4, "tilt", band=0.3, tilt=0.5, tenor_days=7, iv_min=iv)
    print(row(r))

print("\n=== 3. 30D tilt 0.5 IV>=60: IV threshold and band sweeps ===")
print(hdr)
for iv in (50, 70):
    r, _ = simulate(f"30D tilt 0.5, IV>={iv}, band 0.3", H4, "tilt", band=0.3, tilt=0.5, iv_min=iv)
    print(row(r))
for band in (0.2, 0.5):
    r, _ = simulate(f"30D tilt 0.5, IV>=60, band {band}", H4, "tilt", band=band, tilt=0.5, iv_min=60)
    print(row(r))

print("\n=== 4. Roll-timing robustness: start offset shifted 0..5 days (H4 bars 20..50 step 6) ===")
cands = [
    ("A current Renko fixed",        "renko", dict()),
    ("G 30D delta IV>=60",           "delta", dict(band=0.3, iv_min=60)),
    ("I' 30D tilt0.5 IV>=60",        "tilt",  dict(band=0.3, tilt=0.5, iv_min=60)),
    ("N 7D delta IV>=60",            "delta", dict(band=0.3, tenor_days=7, iv_min=60)),
    ("N' 7D tilt0.5 IV>=60",         "tilt",  dict(band=0.3, tilt=0.5, tenor_days=7, iv_min=60)),
]
print(f"{'config':<28}{'mean tot':>9}{'min tot':>9}{'max tot':>9}{'mean DD':>9}{'worst DD':>9}{'mean ret/DD':>12}")
for name, hedge, kw in cands:
    tots, dds = [], []
    for w in range(20, 51, 6):
        r, _ = simulate(name, H4, hedge, warmup_bars=w, **kw)
        tots.append(r["total"]); dds.append(r["mdd"])
    n = len(tots)
    mean_t = sum(tots) / n; mean_dd = sum(dds) / n
    print(f"{name:<28}{mean_t:>9.0f}{min(tots):>9}{max(tots):>9}{mean_dd:>9.0f}{min(dds):>9}{mean_t / abs(mean_dd):>12.2f}")
