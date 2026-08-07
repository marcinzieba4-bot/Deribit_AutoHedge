import os
from decimal import Decimal

import boto3

TABLE_NAME = os.environ["STATE_TABLE"]
_table = boto3.resource("dynamodb").Table(TABLE_NAME)

_DEFAULT_STATE = {
    "pk": "STATE",
    "enabled": False,
    "options_opened": False,
    "hedge_side": None,
    "last_trend": None,
}


def _floats_to_decimals(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _floats_to_decimals(v) for k, v in obj.items()}
    return obj


def _decimals_to_floats(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimals_to_floats(v) for k, v in obj.items()}
    return obj


def get_state():
    resp = _table.get_item(Key={"pk": "STATE"})
    item = resp.get("Item")
    if item is None:
        return dict(_DEFAULT_STATE)
    return _decimals_to_floats(item)


def put_state(state):
    state = dict(state)
    state["pk"] = "STATE"
    _table.put_item(Item=_floats_to_decimals(state))
