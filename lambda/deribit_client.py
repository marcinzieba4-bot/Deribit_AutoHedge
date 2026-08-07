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

    def _public(self, method, params=None):
        resp = requests.get(f"{BASE_URL}/public/{method}", params=params or {}, timeout=self._timeout)
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"Deribit error on public/{method}: {payload['error']}")
        return payload["result"]

    def _private(self, method, params=None):
        token = self._authenticate()
        resp = requests.get(
            f"{BASE_URL}/private/{method}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"Deribit error on private/{method}: {payload['error']}")
        return payload["result"]

    def get_instruments(self, currency, kind, expired=False):
        return self._public(
            "get_instruments",
            {"currency": currency, "kind": kind, "expired": str(expired).lower()},
        )

    def get_index_price(self, index_name):
        return self._public("get_index_price", {"index_name": index_name})["index_price"]

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

    def get_positions(self, currency, kind=None):
        params = {"currency": currency}
        if kind:
            params["kind"] = kind
        return self._private("get_positions", params)

    def market_order(self, instrument_name, side, amount, reduce_only=False):
        method = "buy" if side == "buy" else "sell"
        params = {
            "instrument_name": instrument_name,
            "amount": amount,
            "type": "market",
        }
        if reduce_only:
            params["reduce_only"] = "true"
        return self._private(method, params)
