def _true_range(prev_close, high, low):
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(candles, period):
    """candles: list of {"high","low","close"} ordered oldest to newest."""
    if len(candles) < period + 1:
        raise ValueError("Not enough candles to compute ATR")
    trs = [
        _true_range(candles[i - 1]["close"], candles[i]["high"], candles[i]["low"])
        for i in range(1, len(candles))
    ]
    recent = trs[-period:]
    return sum(recent) / len(recent)


def build_bricks(closes, brick_size):
    """Close-only Renko: a new brick forms every time price moves brick_size
    away from the last brick's base, in either direction (no double-brick
    reversal rule)."""
    if not closes or brick_size <= 0:
        return []
    bricks = []
    base = closes[0]
    for price in closes[1:]:
        while price - base >= brick_size:
            base += brick_size
            bricks.append(1)
        while base - price >= brick_size:
            base -= brick_size
            bricks.append(-1)
    return bricks


def current_trend(closes, brick_size):
    """Direction of the most recently formed (dominant) brick: 'up' or 'down'."""
    bricks = build_bricks(closes, brick_size)
    if not bricks:
        return None
    return "up" if bricks[-1] == 1 else "down"
