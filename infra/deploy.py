#!/usr/bin/env python3
"""Provision (or update) the AWS Secrets Manager secret, IAM role, and Lambda
function for the Deribit autohedge bot. Safe to re-run: creates resources
that don't exist yet, updates the ones that do.

Requires AWS_Key / AWS_Pass (or the standard AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY) in the environment.
"""
import io
import json
import os
import sys
import time
import zipfile

import boto3
import botocore.exceptions

REGION = os.environ.get("AWS_REGION", "eu-west-1")
SECRET_NAME = "deribit-autohedge/api-credentials"
FUNCTION_NAME = "deribit-autohedge"
ROLE_NAME = "deribit-autohedge-lambda-role"
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")

TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
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


def ensure_role(iam, secret_arn):
    try:
        resp = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(TRUST_POLICY),
            Description="Execution role for the deribit-autohedge Lambda",
        )
        role_arn = resp["Role"]["Arn"]
        print(f"Created role: {role_arn}")
        just_created = True
    except iam.exceptions.EntityAlreadyExistsException:
        resp = iam.get_role(RoleName=ROLE_NAME)
        role_arn = resp["Role"]["Arn"]
        print(f"Role already exists: {role_arn}")
        just_created = False

    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="deribit-autohedge-secret-read",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "secretsmanager:GetSecretValue",
                        "Resource": secret_arn,
                    }
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
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(LAMBDA_DIR, "handler.py"), "handler.py")
    buf.seek(0)
    return buf.read()


def ensure_function(lambda_client, role_arn):
    code = build_zip()
    env = {"Variables": {"SECRET_NAME": SECRET_NAME}}

    for attempt in range(5):
        try:
            resp = lambda_client.create_function(
                FunctionName=FUNCTION_NAME,
                Runtime="python3.12",
                Role=role_arn,
                Handler="handler.handler",
                Code={"ZipFile": code},
                Timeout=30,
                MemorySize=128,
                Environment=env,
                Description="Deribit auto-hedge bot (hedging logic pending)",
            )
            print(f"Created function: {resp['FunctionArn']}")
            return
        except lambda_client.exceptions.ResourceConflictException:
            lambda_client.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code)
            waiter = lambda_client.get_waiter("function_updated")
            waiter.wait(FunctionName=FUNCTION_NAME)
            lambda_client.update_function_configuration(
                FunctionName=FUNCTION_NAME,
                Role=role_arn,
                Environment=env,
                Timeout=30,
                MemorySize=128,
            )
            print(f"Updated function: {FUNCTION_NAME}")
            return
        except botocore.exceptions.ClientError as exc:
            if "cannot be assumed" in str(exc) and attempt < 4:
                print("Role not yet assumable by Lambda, retrying...")
                time.sleep(5)
                continue
            raise
    sys.exit("Failed to create function after retries")


def main():
    session = _session()
    secretsmanager = session.client("secretsmanager")
    iam = session.client("iam")
    lambda_client = session.client("lambda")

    secret_arn = ensure_secret(secretsmanager)
    role_arn = ensure_role(iam, secret_arn)
    ensure_function(lambda_client, role_arn)


if __name__ == "__main__":
    main()
