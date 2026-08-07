import json
import os

import boto3

SECRET_NAME = os.environ["SECRET_NAME"]

_secrets_client = boto3.client("secretsmanager")


def _get_deribit_credentials():
    response = _secrets_client.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(response["SecretString"])


def handler(event, context):
    credentials = _get_deribit_credentials()
    # Hedging logic pending.
    return {"status": "not_implemented"}
