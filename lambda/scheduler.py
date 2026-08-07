import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

SCHEDULE_NAME = os.environ.get("SCHEDULE_NAME", "deribit-autohedge-tick")
FUNCTION_ARN = os.environ["FUNCTION_ARN"]
SCHEDULER_ROLE_ARN = os.environ["SCHEDULER_ROLE_ARN"]
TICK_BUFFER_MINUTES = int(os.environ.get("TICK_BUFFER_MINUTES", "5"))

_client = boto3.client("scheduler")


def _next_h4_boundary(now):
    next_block = ((now.hour // 4) + 1) * 4
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if next_block >= 24:
        return day_start + timedelta(days=1)
    return day_start + timedelta(hours=next_block)


def schedule_next_tick():
    now = datetime.now(timezone.utc)
    fire_at = _next_h4_boundary(now) + timedelta(minutes=TICK_BUFFER_MINUTES)
    payload = dict(
        Name=SCHEDULE_NAME,
        ScheduleExpression=f"at({fire_at.strftime('%Y-%m-%dT%H:%M:%S')})",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": FUNCTION_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"action": "tick"}),
        },
    )
    try:
        _client.update_schedule(**payload)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        _client.create_schedule(**payload)
    return fire_at.isoformat()


def cancel_next_tick():
    try:
        _client.delete_schedule(Name=SCHEDULE_NAME)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
