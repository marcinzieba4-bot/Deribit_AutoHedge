import time

import requests

BASE_URL = "https://www.deribit.com/api/v2"


class DeribitClient:
    def __init__(self, client_id, client_secret, timeout=15):
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token = None
        self._token_expiry = 0

    def _authenticate(self):
        if self._token and time.time() < self._token_expiry:
            return self._token
        resp = requests.get(
            f"{BASE_URL}/public/auth",
            params={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=self._timeout,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        self._token = result["access_token"]
        self._token_expiry = time.time() + result["expires_in"] - 30
        return self._token

    def _call(self, path, params, headers=None):
        resp = requests.get(f"{BASE_URL}/{path}", params=params or {}, headers=headers, timeout=self._timeout)
        payload = resp.json() if resp.content else {}
        if "error" in payload:
            raise RuntimeError(f"Deribit error on {path}: {payload['error']}")
        resp.raise_for_status()
        return payload["result"]

    def _public(self, method, params=None):
        return self._call(f"public/{method}", params)

    def _private(self, method, params=None):
        return self._call(f"private/{method}", params, {"Authorization": f"Bearer {self._authenticate()}"})

    # --- market data ---

    def get_instruments(self, currency, kind, expired=False):
        return self._public(
            "get_instruments",
            {"currency": currency, "kind": kind, "expired": str(expired).lower()},
        )

    def get_instrument(self, instrument_name):
        return self._public("get_instrument", {"instrument_name": instrument_name})

    def get_index_price(self, index_name):
        return self._public("get_index_price", {"index_name": index_name})["index_price"]

    def get_ticker(self, instrument_name):
        return self._public("ticker", {"instrument_name": instrument_name})

    def get_dvol(self, currency="ETH"):
        now = int(time.time() * 1000)
        data = self._public(
            "get_volatility_index_data",
            {"currency": currency, "start_timestamp": now - 3 * 3600 * 1000, "end_timestamp": now, "resolution": 3600},
        )["data"]
        return data[-1][4]

    def get_tradingview_chart_data(self, instrument_name, resolution, start_ms, end_ms):
        return self._public(
            "get_tradingview_chart_data",
            {
                "instrument_name": instrument_name,
                "start_timestamp": int(start_ms),
                "end_timestamp": int(end_ms),
                "resolution": resolution,
            },
        )

    # --- account ---

    def get_positions(self, currency, kind=None):
        params = {"currency": currency}
        if kind:
            params["kind"] = kind
        return self._private("get_positions", params)

    # --- orders ---

    def limit_order(self, instrument_name, side, amount, price, post_only=True):
        params = {
            "instrument_name": instrument_name,
            "amount": amount,
            "type": "limit",
            "price": price,
            "post_only": str(post_only).lower(),
        }
        return self._private("buy" if side == "buy" else "sell", params)

    def market_order(self, instrument_name, side, amount):
        params = {"instrument_name": instrument_name, "amount": amount, "type": "market"}
        return self._private("buy" if side == "buy" else "sell", params)

    def get_order_state(self, order_id):
        return self._private("get_order_state", {"order_id": order_id})

    def cancel_order(self, order_id):
        return self._private("cancel", {"order_id": order_id})
