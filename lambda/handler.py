import json
import os

import boto3

import strategy

SECRET_NAME = os.environ["SECRET_NAME"]
_secrets_client = boto3.client("secretsmanager")


def _get_deribit_credentials():
    response = _secrets_client.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(response["SecretString"])


def handler(event, context):
    action = (event or {}).get("action", "tick")

    if action == "start":
        return strategy.start(_get_deribit_credentials())
    if action == "stop":
        return strategy.stop()
    if action == "tick":
        return strategy.tick(_get_deribit_credentials())

    raise ValueError(f"Unknown action: {action}")
