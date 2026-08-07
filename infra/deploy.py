#!/usr/bin/env python3
"""Provision (or update) all AWS resources for the Deribit autohedge bot:
Secrets Manager secret, DynamoDB state table, IAM roles, Lambda function
(with dependencies bundled), and EventBridge Scheduler permissions for the
Lambda's self-rescheduling loop. Safe to re-run: creates resources that
don't exist yet, updates the ones that do.

Requires AWS_Key / AWS_Pass (or the standard AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY) in the environment.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import boto3
import botocore.exceptions

REGION = os.environ.get("AWS_REGION", "eu-west-1")
SECRET_NAME = "deribit-autohedge/api-credentials"
FUNCTION_NAME = "deribit-autohedge"
LAMBDA_ROLE_NAME = "deribit-autohedge-lambda-role"
SCHEDULER_ROLE_NAME = "deribit-autohedge-scheduler-role"
STATE_TABLE_NAME = "deribit-autohedge-state"
SCHEDULE_NAME = "deribit-autohedge-tick"
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")

LAMBDA_ENV_DEFAULTS = {
    "SECRET_NAME": SECRET_NAME,
    "STATE_TABLE": STATE_TABLE_NAME,
    "SCHEDULE_NAME": SCHEDULE_NAME,
    "PERP_INSTRUMENT": "ETH_USDC-PERPETUAL",
    "DEFAULT_SIZE": "2",
    "EXPIRY_TARGET_DAYS": "30",
    "ATR_PERIOD": "14",
    "ATR_MULTIPLIER": "1.0",
    "H4_LOOKBACK_DAYS": "90",
    "TICK_BUFFER_MINUTES": "5",
}

LAMBDA_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ],
}

SCHEDULER_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Principal": {"Service": "scheduler.amazonaws.com"}, "Action": "sts:AssumeRole"}
    ],
}


def _session():
    access_key = os.environ.get("AWS_Key") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_Pass") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    return boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=REGION,
    )


def ensure_secret(secretsmanager):
    try:
        resp = secretsmanager.create_secret(
            Name=SECRET_NAME,
            Description="Deribit API credentials for the autohedge Lambda",
            SecretString=json.dumps({"client_id": "", "client_secret": ""}),
        )
        print(f"Created secret: {resp['ARN']}")
        return resp["ARN"]
    except secretsmanager.exceptions.ResourceExistsException:
        resp = secretsmanager.describe_secret(SecretId=SECRET_NAME)
        print(f"Secret already exists: {resp['ARN']}")
        return resp["ARN"]


def ensure_state_table(dynamodb):
    try:
        resp = dynamodb.create_table(
            TableName=STATE_TABLE_NAME,
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        arn = resp["TableDescription"]["TableArn"]
        dynamodb.get_waiter("table_exists").wait(TableName=STATE_TABLE_NAME)
        print(f"Created state table: {arn}")
        return arn
    except dynamodb.exceptions.ResourceInUseException:
        resp = dynamodb.describe_table(TableName=STATE_TABLE_NAME)
        arn = resp["Table"]["TableArn"]
        print(f"State table already exists: {arn}")
        return arn


def ensure_scheduler_role(iam, function_arn):
    try:
        resp = iam.create_role(
            RoleName=SCHEDULER_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(SCHEDULER_TRUST_POLICY),
            Description="Lets EventBridge Scheduler invoke the deribit-autohedge Lambda",
        )
        role_arn = resp["Role"]["Arn"]
        print(f"Created scheduler role: {role_arn}")
        just_created = True
    except iam.exceptions.EntityAlreadyExistsException:
        resp = iam.get_role(RoleName=SCHEDULER_ROLE_NAME)
        role_arn = resp["Role"]["Arn"]
        print(f"Scheduler role already exists: {role_arn}")
        just_created = False

    iam.put_role_policy(
        RoleName=SCHEDULER_ROLE_NAME,
        PolicyName="invoke-deribit-autohedge",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": function_arn}
                ],
            }
        ),
    )
    if just_created:
        time.sleep(10)
    return role_arn


def ensure_lambda_role(iam, secret_arn, table_arn, scheduler_role_arn, account_id):
    try:
        resp = iam.create_role(
            RoleName=LAMBDA_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST_POLICY),
            Description="Execution role for the deribit-autohedge Lambda",
        )
        role_arn = resp["Role"]["Arn"]
        print(f"Created role: {role_arn}")
        just_created = True
    except iam.exceptions.EntityAlreadyExistsException:
        resp = iam.get_role(RoleName=LAMBDA_ROLE_NAME)
        role_arn = resp["Role"]["Arn"]
        print(f"Role already exists: {role_arn}")
        just_created = False

    iam.attach_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=LAMBDA_ROLE_NAME,
        PolicyName="deribit-autohedge-access",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": secret_arn,
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                        "Resource": table_arn,
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "scheduler:CreateSchedule",
                            "scheduler:UpdateSchedule",
                            "scheduler:GetSchedule",
                            "scheduler:DeleteSchedule",
                        ],
                        "Resource": f"arn:aws:scheduler:{REGION}:{account_id}:schedule/default/{SCHEDULE_NAME}",
                    },
                    {
                        "Effect": "Allow",
                        "Action": "iam:PassRole",
                        "Resource": scheduler_role_arn,
                    },
                ],
            }
        ),
    )
    if just_created:
        # New roles aren't immediately assumable everywhere; give IAM a moment
        # to propagate before Lambda tries to use it.
        time.sleep(10)
    return role_arn


def build_zip():
    build_dir = tempfile.mkdtemp(prefix="deribit-autohedge-")
    try:
        requirements = os.path.join(LAMBDA_DIR, "requirements.txt")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--target", build_dir, "-r", requirements],
            check=True,
        )
        for name in os.listdir(LAMBDA_DIR):
            if name.endswith(".py"):
                shutil.copy(os.path.join(LAMBDA_DIR, name), build_dir)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(build_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    zf.write(full_path, os.path.relpath(full_path, build_dir))
        buf.seek(0)
        return buf.read()
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def ensure_function(lambda_client, role_arn):
    code = build_zip()
    env = {"Variables": LAMBDA_ENV_DEFAULTS}

    for attempt in range(5):
        try:
            resp = lambda_client.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime="python3.12",
                Role=role_arn,
                Handler="handler.handler",
                Code={"ZipFile": code},
                Timeout=60,
                MemorySize=256,
                Environment=env,
                Description="Deribit auto-hedge bot",
            )
            print(f"Created function: {resp['FunctionArn']}")
            return resp["FunctionArn"]
        except lambda_client.exceptions.ResourceConflictException:
            lambda_client.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code)
            lambda_client.get_waiter("function_updated").wait(FunctionName=FUNCTION_NAME)
            lambda_client.update_function_configuration(
                FunctionName=FUNCTION_NAME,
                Role=role_arn,
                Timeout=60,
                MemorySize=256,
            )
            lambda_client.get_waiter("function_updated").wait(FunctionName=FUNCTION_NAME)
            resp = lambda_client.get_function(FunctionName=FUNCTION_NAME)
            print(f"Updated function: {FUNCTION_NAME}")
            return resp["Configuration"]["FunctionArn"]
        except botocore.exceptions.ClientError as exc:
            if "cannot be assumed" in str(exc) and attempt < 4:
                print("Role not yet assumable by Lambda, retrying...")
                time.sleep(5)
                continue
            raise
    sys.exit("Failed to create function after retries")


def finalize_function_env(lambda_client, function_arn, scheduler_role_arn):
    env_vars = dict(LAMBDA_ENV_DEFAULTS)
    env_vars["FUNCTION_ARN"] = function_arn
    env_vars["SCHEDULER_ROLE_ARN"] = scheduler_role_arn
    lambda_client.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Environment={"Variables": env_vars},
    )
    lambda_client.get_waiter("function_updated").wait(FunctionName=FUNCTION_NAME)
    print("Set FUNCTION_ARN / SCHEDULER_ROLE_ARN on the function")


def ensure_recursion_allowed(lambda_client):
    lambda_client.put_function_recursion_config(FunctionName=FUNCTION_NAME, RecursiveLoop="Allow")
    print("Set recursive-loop detection to Allow (required for the self-rescheduling tick loop)")


def main():
    session = _session()
    secretsmanager = session.client("secretsmanager")
    dynamodb = session.client("dynamodb")
    iam = session.client("iam")
    lambda_client = session.client("lambda")

    secret_arn = ensure_secret(secretsmanager)
    table_arn = ensure_state_table(dynamodb)

    # Function ARN is deterministic (account/region/name), so the scheduler
    # role's invoke policy can be created before the function exists.
    account_id = session.client("sts").get_caller_identity()["Account"]
    function_arn = f"arn:aws:lambda:{REGION}:{account_id}:function:{FUNCTION_NAME}"

    scheduler_role_arn = ensure_scheduler_role(iam, function_arn)
    lambda_role_arn = ensure_lambda_role(iam, secret_arn, table_arn, scheduler_role_arn, account_id)

    real_function_arn = ensure_function(lambda_client, lambda_role_arn)
    assert real_function_arn == function_arn, (real_function_arn, function_arn)

    finalize_function_env(lambda_client, function_arn, scheduler_role_arn)
    ensure_recursion_allowed(lambda_client)


if __name__ == "__main__":
    main()
