import json
import logging
import os
import traceback

import boto3

import scheduler
import state as state_store
import strategy

log = logging.getLogger()
log.setLevel(logging.INFO)

SECRET_NAME = os.environ["SECRET_NAME"]
_secrets_client = boto3.client("secretsmanager")


def _get_deribit_credentials():
    response = _secrets_client.get_secret_value(SecretId=SECRET_NAME)
    return json.loads(response["SecretString"])


def handler(event, context):
    action = (event or {}).get("action", "tick")
    result = None
    try:
        if action == "start":
            result = strategy.start(_get_deribit_credentials())
        elif action == "stop":
            result = strategy.stop()
            scheduler.cancel_next_tick()
        elif action == "tick":
            result = strategy.tick(_get_deribit_credentials())
        elif action == "status":
            result = strategy.status(_get_deribit_credentials())
        else:
            raise ValueError(f"Unknown action: {action}")
    except Exception as exc:  # never raise: a raised error would trigger retries and skip the reschedule
        log.error("action %s failed: %s\n%s", action, exc, traceback.format_exc())
        result = {"status": "error", "action": action, "error": str(exc)}
        try:
            st = state_store.get_state()
            st["last_error"] = f"{strategy._now_iso()} {exc}"
            state_store.put_state(st)
        except Exception as inner:
            log.error("could not record error in state: %s", inner)
    finally:
        if action in ("start", "tick"):
            try:
                if state_store.get_state().get("enabled"):
                    result["next_tick"] = scheduler.schedule_next_tick()
            except Exception as exc:
                log.error("reschedule failed: %s", exc)
    log.info("result: %s", json.dumps(result, default=str))
    return result
