import os
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
import json
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lambda"))
from deribit_client import DeribitClient  # noqa: E402

client = DeribitClient("", "")

NOW = int(time.time() * 1000)
THREE_YEARS_MS = 3 * 365 * 24 * 3600 * 1000
START = NOW - THREE_YEARS_MS

INSTRUMENT = "BTC_USDC-PERPETUAL"


def fetch_chunked_candles():
    print("Fetching 1H candles...")
    all_ticks = {}
    chunk = 80 * 24 * 3600 * 1000  # ~80 days per call, safely under the 5001-tick cap
    cursor = START
    while cursor < NOW:
        end = min(cursor + chunk, NOW)
        data = client.get_tradingview_chart_data(INSTRUMENT, "60", cursor, end)
        if data.get("status") == "ok" and data.get("ticks"):
            for i, ts in enumerate(data["ticks"]):
                all_ticks[ts] = {
                    "high": data["high"][i],
                    "low": data["low"][i],
                    "close": data["close"][i],
                }
        print(f"  {cursor} -> {end}: {len(data.get('ticks') or [])} ticks (total {len(all_ticks)})")
        cursor = end
        time.sleep(0.1)
    return all_ticks


def fetch_chunked_funding():
    print("Fetching funding rate history...")
    all_rows = {}
    chunk = 25 * 24 * 3600 * 1000  # ~25 days per call, under the ~31-day/744-row cap
    cursor = START
    while cursor < NOW:
        end = min(cursor + chunk, NOW)
        rows = client._public(
            "get_funding_rate_history",
            {"instrument_name": INSTRUMENT, "start_timestamp": cursor, "end_timestamp": end},
        )
        for r in rows:
            all_rows[r["timestamp"]] = r["interest_1h"]
        print(f"  {cursor} -> {end}: {len(rows)} rows (total {len(all_rows)})")
        cursor = end
        time.sleep(0.1)
    return all_rows


def fetch_chunked_dvol():
    print("Fetching DVOL...")
    all_rows = {}
    chunk = 25 * 24 * 3600 * 1000
    cursor = START
    while cursor < NOW:
        end = min(cursor + chunk, NOW)
        resp = client._public(
            "get_volatility_index_data",
            {"currency": "BTC", "start_timestamp": cursor, "end_timestamp": end, "resolution": 3600},
        )
        rows = resp.get("data", []) if isinstance(resp, dict) else resp
        for row in rows:
            ts, o, h, l, c = row
            all_rows[ts] = c
        print(f"  {cursor} -> {end}: {len(rows)} rows (total {len(all_rows)})")
        cursor = end
        time.sleep(0.1)
    return all_rows


candles = fetch_chunked_candles()
funding = fetch_chunked_funding()
dvol = fetch_chunked_dvol()

with open(os.path.join(DATA_DIR, "btc_candles_1h.json"), "w") as f:
    json.dump(candles, f)
with open(os.path.join(DATA_DIR, "btc_funding_1h.json"), "w") as f:
    json.dump(funding, f)
with open(os.path.join(DATA_DIR, "btc_dvol_1h.json"), "w") as f:
    json.dump(dvol, f)

print("Done.", len(candles), "candles,", len(funding), "funding rows,", len(dvol), "dvol rows")
