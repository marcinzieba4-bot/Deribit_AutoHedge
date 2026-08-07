import os
import time
from datetime import datetime, timezone

import scheduler as scheduler_store
import state as state_store
from candles import fetch_h4_candles
from deribit_client import DeribitClient
from renko import average_true_range, current_trend

CURRENCY = "ETH"
PERP_INSTRUMENT = os.environ.get("PERP_INSTRUMENT", "ETH_USDC-PERPETUAL")
DEFAULT_SIZE = float(os.environ.get("DEFAULT_SIZE", "2"))
EXPIRY_TARGET_DAYS = int(os.environ.get("EXPIRY_TARGET_DAYS", "30"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", "1.0"))
H4_LOOKBACK_DAYS = int(os.environ.get("H4_LOOKBACK_DAYS", "90"))


def _client(credentials):
    return DeribitClient(credentials["client_id"], credentials["client_secret"])


def _compute_trend(client):
    candles = fetch_h4_candles(client, PERP_INSTRUMENT, H4_LOOKBACK_DAYS)
    closes = [c["close"] for c in candles]
    brick_size = average_true_range(candles, ATR_PERIOD) * ATR_MULTIPLIER
    trend = current_trend(closes, brick_size)
    if trend is None:
        raise RuntimeError("Unable to determine Renko trend from available candles")
    return trend, brick_size


def _pick_atm_options(client):
    instruments = client.get_instruments(CURRENCY, "option", expired=False)
    target_ms = time.time() * 1000 + EXPIRY_TARGET_DAYS * 24 * 3600 * 1000
    expiries = sorted({i["expiration_timestamp"] for i in instruments})
    expiry = min(expiries, key=lambda e: abs(e - target_ms))

    index_price = client.get_index_price("eth_usd")
    same_expiry = [i for i in instruments if i["expiration_timestamp"] == expiry]
    strikes = sorted({i["strike"] for i in same_expiry})
    atm_strike = min(strikes, key=lambda s: abs(s - index_price))

    call = next(i["instrument_name"] for i in same_expiry if i["strike"] == atm_strike and i["option_type"] == "call")
    put = next(i["instrument_name"] for i in same_expiry if i["strike"] == atm_strike and i["option_type"] == "put")
    return put, call


def start(credentials):
    st = state_store.get_state()
    if st.get("enabled"):
        return {"status": "already_running", "state": st}

    client = _client(credentials)

    put_instrument, call_instrument = _pick_atm_options(client)
    put_order = client.market_order(put_instrument, "sell", DEFAULT_SIZE)
    call_order = client.market_order(call_instrument, "sell", DEFAULT_SIZE)

    trend, brick_size = _compute_trend(client)
    hedge_side = "buy" if trend == "up" else "sell"
    perp_order = client.market_order(PERP_INSTRUMENT, hedge_side, DEFAULT_SIZE)

    st.update(
        enabled=True,
        options_opened=True,
        put_instrument=put_instrument,
        call_instrument=call_instrument,
        hedge_side=hedge_side,
        last_trend=trend,
        brick_size=brick_size,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    state_store.put_state(st)
    next_tick = scheduler_store.schedule_next_tick()

    return {
        "status": "started",
        "put_instrument": put_instrument,
        "call_instrument": call_instrument,
        "put_order": put_order.get("order", {}).get("order_id"),
        "call_order": call_order.get("order", {}).get("order_id"),
        "perp_order": perp_order.get("order", {}).get("order_id"),
        "hedge_side": hedge_side,
        "next_tick": next_tick,
    }


def stop():
    st = state_store.get_state()
    st["enabled"] = False
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_store.put_state(st)
    scheduler_store.cancel_next_tick()
    return {"status": "stopped"}


def tick(credentials):
    st = state_store.get_state()
    if not st.get("enabled"):
        return {"status": "disabled"}

    client = _client(credentials)
    trend, brick_size = _compute_trend(client)
    result = {"status": "checked", "trend": trend, "previous_trend": st.get("last_trend")}

    if trend != st.get("last_trend"):
        new_side = "buy" if trend == "up" else "sell"
        old_side = st.get("hedge_side")
        if old_side:
            close_side = "sell" if old_side == "buy" else "buy"
            client.market_order(PERP_INSTRUMENT, close_side, DEFAULT_SIZE, reduce_only=True)
        perp_order = client.market_order(PERP_INSTRUMENT, new_side, DEFAULT_SIZE)
        st["hedge_side"] = new_side
        result["flipped_to"] = new_side
        result["perp_order"] = perp_order.get("order", {}).get("order_id")

    st["last_trend"] = trend
    st["brick_size"] = brick_size
    st["updated_at"] = datetime.now(timezone.utc).isoformat()
    state_store.put_state(st)

    result["next_tick"] = scheduler_store.schedule_next_tick()
    return result
