def _true_range(prev_close, high, low):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def _wilder_atr_series(candles, period):
    """candle index -> ATR(period), Wilder-smoothed (matches TradingView's
    ta.atr()), computed using data up to and including that candle."""
    trs = [
        _true_range(candles[i - 1]["close"], candles[i]["high"], candles[i]["low"])
        for i in range(1, len(candles))
    ]
    if len(trs) < period:
        return {}
    atrs = {}
    seed = sum(trs[:period]) / period
    atrs[period] = seed
    prev = seed
    for i in range(period, len(trs)):
        prev = (prev * (period - 1) + trs[i]) / period
        atrs[i + 1] = prev
    return atrs


def compute_trend(candles, atr_period=14, atr_mult=0.15, tick_trend=2, tick_reversal=4):
    """Replicates "Universal Renko Bars by SiddWolf" (ATR Based calculation
    method, Open Offset 0): at most one brick confirms per bar, the
    confirmed brick's close snaps to the crossed threshold (not the actual
    close), and continuation vs. reversal thresholds are asymmetric
    (tick_trend ticks to continue, tick_reversal ticks to flip).

    candles: H4 candles (oldest to newest), each {"high","low","close"}.
    Returns (trend, tick_size) where trend is "up", "down", or None if no
    brick has confirmed yet.
    """
    atrs = _wilder_atr_series(candles, atr_period)
    if not atrs:
        return None, None
    start = min(atrs.keys())

    bar_close = candles[start]["close"]
    tick_size = atrs[start] * atr_mult
    bar_max = bar_close + tick_trend * tick_size
    bar_min = bar_close - tick_trend * tick_size
    direction = 0

    for t in range(start + 1, len(candles)):
        if t not in atrs:
            continue
        price = candles[t]["close"]
        tick_size = atrs[t] * atr_mult
        if price > bar_max:
            direction = 1
            this_close = bar_max
            bar_max = this_close + tick_trend * tick_size
            bar_min = this_close - tick_reversal * tick_size
        elif price < bar_min:
            direction = -1
            this_close = bar_min
            bar_max = this_close + tick_reversal * tick_size
            bar_min = this_close - tick_trend * tick_size

    if direction == 0:
        return None, tick_size
    return ("up" if direction == 1 else "down"), tick_size
