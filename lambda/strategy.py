import logging
import math
import os
import time
from datetime import datetime, timezone

import state as state_store
from candles import fetch_h4_candles
from deribit_client import DeribitClient
from renko import compute_trend

log = logging.getLogger()

CURRENCY = "ETH"
PERP_INSTRUMENT = os.environ.get("PERP_INSTRUMENT", "ETH_USDC-PERPETUAL")
SIZE = float(os.environ.get("DEFAULT_SIZE", "1"))
EXPIRY_TARGET_DAYS = int(os.environ.get("EXPIRY_TARGET_DAYS", "30"))
IV_MIN = float(os.environ.get("IV_MIN", "60"))
DELTA_BAND = float(os.environ.get("DELTA_BAND", "0.3"))
TILT = float(os.environ.get("TILT", "0.5"))
MOMENTUM_SIZE = float(os.environ.get("MOMENTUM_SIZE", "0"))
LIMIT_WAIT_SECONDS = int(os.environ.get("LIMIT_WAIT_SECONDS", "60"))
ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
ATR_MULTIPLIER = float(os.environ.get("ATR_MULTIPLIER", "0.15"))
TICK_TREND = int(os.environ.get("TICK_TREND", "2"))
TICK_REVERSAL = int(os.environ.get("TICK_REVERSAL", "4"))
H4_LOOKBACK_DAYS = int(os.environ.get("H4_LOOKBACK_DAYS", "180"))

PERP_AMOUNT_DECIMALS = 4


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _client(credentials):
    return DeribitClient(credentials["client_id"], credentials["client_secret"])


# --- execution: post at mid (passive side), fall back to market ---

def _round_to_tick(price, tick, side):
    steps = price / tick
    steps = math.floor(steps) if side == "buy" else math.ceil(steps)
    return round(steps * tick, 10)


def execute(client, instrument_name, side, amount):
    """Maker-first fill: limit at the passive-rounded mid, wait, then market the rest."""
    tick = client.get_instrument(instrument_name)["tick_size"]
    ticker = client.get_ticker(instrument_name)
    bid, ask = ticker.get("best_bid_price"), ticker.get("best_ask_price")
    filled = 0.0
    limit_id = None
    if bid and ask:
        price = _round_to_tick((bid + ask) / 2, tick, side)
        if (side == "buy" and price < ask) or (side == "sell" and price > bid):
            try:
                limit_id = client.limit_order(instrument_name, side, amount, price)["order"]["order_id"]
            except RuntimeError as exc:
                log.warning("limit order rejected on %s: %s", instrument_name, exc)
    if limit_id:
        deadline = time.time() + LIMIT_WAIT_SECONDS
        while time.time() < deadline:
            order = client.get_order_state(limit_id)
            filled = order.get("filled_amount", 0.0)
            if order["order_state"] == "filled":
                return {"instrument": instrument_name, "side": side, "amount": amount, "limit_filled": filled, "market_filled": 0.0}
            time.sleep(5)
        try:
            client.cancel_order(limit_id)
        except RuntimeError as exc:
            log.warning("cancel failed (probably filled meanwhile): %s", exc)
        filled = client.get_order_state(limit_id).get("filled_amount", 0.0)
    remaining = round(amount - filled, PERP_AMOUNT_DECIMALS)
    market_filled = 0.0
    if remaining > 0:
        client.market_order(instrument_name, side, remaining)
        market_filled = remaining
    return {"instrument": instrument_name, "side": side, "amount": amount, "limit_filled": filled, "market_filled": market_filled}


# --- exchange state ---

def perp_position(client):
    for p in client.get_positions("USDC", kind="future"):
        if p["instrument_name"] == PERP_INSTRUMENT:
            size = float(p.get("size_currency") or 0.0)
            return size if p["direction"] == "buy" else -size
    return 0.0


def option_position(client, instrument_name):
    for p in client.get_positions(CURRENCY, kind="option"):
        if p["instrument_name"] == instrument_name:
            return float(p.get("size") or 0.0)
    return 0.0


def portfolio_delta(client, put_instrument, call_instrument):
    """ETH delta of the short straddle, using Deribit's own greeks."""
    dp = client.get_ticker(put_instrument)["greeks"]["delta"]
    dc = client.get_ticker(call_instrument)["greeks"]["delta"]
    return -SIZE * (dp + dc)


def renko_trend(client):
    trend, tick_size = compute_trend(
        fetch_h4_candles(client, PERP_INSTRUMENT, H4_LOOKBACK_DAYS),
        ATR_PERIOD, ATR_MULTIPLIER, TICK_TREND, TICK_REVERSAL,
    )
    return trend, tick_size


def pick_atm_options(client):
    instruments = client.get_instruments(CURRENCY, "option", expired=False)
    target_ms = time.time() * 1000 + EXPIRY_TARGET_DAYS * 24 * 3600 * 1000
    expiry = min({i["expiration_timestamp"] for i in instruments}, key=lambda e: abs(e - target_ms))
    index_price = client.get_index_price("eth_usd")
    same_expiry = [i for i in instruments if i["expiration_timestamp"] == expiry]
    atm_strike = min({i["strike"] for i in same_expiry}, key=lambda s: abs(s - index_price))
    call = next(i["instrument_name"] for i in same_expiry if i["strike"] == atm_strike and i["option_type"] == "call")
    put = next(i["instrument_name"] for i in same_expiry if i["strike"] == atm_strike and i["option_type"] == "put")
    return put, call, expiry


# --- strategy steps ---

def _reconcile_options(client, st, result):
    """Mark the straddle closed if it expired or is no longer on the exchange."""
    if not st.get("options_open"):
        return
    expired = st.get("options_expiry", 0) <= time.time() * 1000
    still_held = option_position(client, st["put_instrument"]) != 0 or option_position(client, st["call_instrument"]) != 0
    if expired or not still_held:
        st["options_open"] = False
        result["options_closed"] = "expired" if expired else "not_on_exchange"


def _maybe_open_options(client, st, result):
    if st.get("options_open"):
        return
    dvol = client.get_dvol(CURRENCY)
    result["dvol"] = dvol
    st["last_dvol"] = dvol
    if dvol < IV_MIN:
        result["options"] = f"skipped: DVOL {dvol} < {IV_MIN}"
        return
    put, call, expiry = pick_atm_options(client)
    result["put_fill"] = execute(client, put, "sell", SIZE)
    result["call_fill"] = execute(client, call, "sell", SIZE)
    st.update(options_open=True, put_instrument=put, call_instrument=call, options_expiry=expiry, options_opened_at=_now_iso())
    result["options"] = f"opened {put} / {call}"


def _rebalance_hedge(client, st, result):
    if st.get("options_open"):
        pdelta = portfolio_delta(client, st["put_instrument"], st["call_instrument"])
        trend, tick_size = renko_trend(client)
        tilt = TILT * (1 if trend == "up" else -1 if trend == "down" else 0)
        target = -pdelta + tilt
        st.update(last_trend=trend, tick_size=tick_size, last_portfolio_delta=pdelta)
        result.update(portfolio_delta=round(pdelta, 4), trend=trend)
    else:
        # Low-vol regime book: Renko momentum on the perp while no straddle is open.
        # Off by default (MOMENTUM_SIZE=0); backtested at 1 ETH, DVOL<60.
        low_vol = st.get("last_dvol") is not None and st["last_dvol"] < IV_MIN
        if MOMENTUM_SIZE > 0 and low_vol:
            trend, tick_size = renko_trend(client)
            st.update(last_trend=trend, tick_size=tick_size)
            target = MOMENTUM_SIZE * (1 if trend == "up" else -1 if trend == "down" else 0)
            result.update(momentum=f"{trend} (DVOL {st['last_dvol']} < {IV_MIN})", trend=trend)
        else:
            target = 0.0
    current = perp_position(client)
    diff = round(target - current, PERP_AMOUNT_DECIMALS)
    result.update(hedge_target=round(target, 4), hedge_current=current)
    if abs(diff) < DELTA_BAND and not (target == 0.0 and current != 0.0):
        result["hedge"] = "within band"
    elif diff != 0:
        result["hedge_fill"] = execute(client, PERP_INSTRUMENT, "buy" if diff > 0 else "sell", abs(diff))
        current = perp_position(client)
    st["last_perp_position"] = current
    st["last_hedge_target"] = target


def tick(credentials):
    st = state_store.get_state()
    if not st.get("enabled"):
        return {"status": "disabled"}
    client = _client(credentials)
    result = {"status": "checked"}
    try:
        _reconcile_options(client, st, result)
        _maybe_open_options(client, st, result)
        _rebalance_hedge(client, st, result)
        st["last_error"] = None
    finally:
        st["updated_at"] = _now_iso()
        state_store.put_state(st)
    return result


def start(credentials):
    st = state_store.get_state()
    st["enabled"] = True
    st["updated_at"] = _now_iso()
    state_store.put_state(st)
    result = tick(credentials)
    result["status"] = "started"
    return result


def stop():
    """Disables the loop. Positions are left as they are."""
    st = state_store.get_state()
    st["enabled"] = False
    st["updated_at"] = _now_iso()
    state_store.put_state(st)
    return {"status": "stopped"}


def status(credentials):
    st = state_store.get_state()
    client = _client(credentials)
    out = {"state": st, "perp_position": perp_position(client), "dvol": client.get_dvol(CURRENCY)}
    if st.get("options_open"):
        out["portfolio_delta"] = portfolio_delta(client, st["put_instrument"], st["call_instrument"])
    return out
