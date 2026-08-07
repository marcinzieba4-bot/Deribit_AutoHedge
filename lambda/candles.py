from datetime import datetime, timedelta, timezone

H4_MS = 4 * 60 * 60 * 1000


def fetch_h4_candles(client, instrument_name, lookback_days):
    """Deribit has no native 4H resolution, so fetch 1H candles and aggregate
    into 4H buckets aligned to 00:00/04:00/... UTC. Only fully-closed buckets
    are returned."""
    now = datetime.now(timezone.utc)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=lookback_days)

    data = client.get_tradingview_chart_data(
        instrument_name,
        "60",
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )
    if data.get("status") != "ok" or not data.get("ticks"):
        raise RuntimeError(f"No candle data returned: {data}")

    buckets = {}
    for i, ts in enumerate(data["ticks"]):
        bucket_ts = ts - (ts % H4_MS)
        bucket = buckets.setdefault(
            bucket_ts, {"high": data["high"][i], "low": data["low"][i], "close": data["close"][i]}
        )
        bucket["high"] = max(bucket["high"], data["high"][i])
        bucket["low"] = min(bucket["low"], data["low"][i])
        bucket["close"] = data["close"][i]  # ticks arrive oldest-first, so last write wins

    ordered_keys = sorted(buckets.keys())
    now_bucket = int(now.timestamp() * 1000)
    now_bucket -= now_bucket % H4_MS
    if ordered_keys and ordered_keys[-1] >= now_bucket:
        ordered_keys = ordered_keys[:-1]

    return [buckets[k] for k in ordered_keys]
