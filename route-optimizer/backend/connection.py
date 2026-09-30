"""
connection.py

Runs a query against AWS Athena and returns the results as a list of
plain dicts (column name -> string value, Athena's own representation —
callers that need numbers already do float()/int() on these, e.g.
battery_pickups._rows_to_stations).

No credentials are hardcoded here. boto3 resolves them the standard way
(environment variables, an AWS CLI profile, or an IAM role) — see
backend/ENV_SETUP.md for exactly what to set.
"""

import os
import time

import boto3

def _local_credentials():
    """Falls back to backend/credentials.py for local dev convenience if
    the standard boto3 env vars aren't set. Never required in a real
    deployment — set the env vars there instead."""
    try:
        import credentials
        return credentials
    except ImportError:
        return None


_creds = _local_credentials()

AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID") or getattr(_creds, "AWS_ACCESS_KEY_ID", None)
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY") or getattr(_creds, "AWS_SECRET_ACCESS_KEY", None)
AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN") or getattr(_creds, "AWS_SESSION_TOKEN", None)
AWS_REGION = os.environ.get("AWS_REGION") or getattr(_creds, "AWS_REGION", "us-east-1")
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP") or getattr(_creds, "ATHENA_WORKGROUP", "primary")
# Only needed if your workgroup doesn't already have a default query
# result location configured in the AWS console.
ATHENA_OUTPUT_S3 = os.environ.get("ATHENA_OUTPUT_S3") or getattr(_creds, "S3_STAGING_DIR", "")


def run_athena_query(query, timeout_seconds=60, poll_interval=1.0):
    """
    Runs `query` on Athena, waits for it to finish, and returns every
    result row as a dict of {column_name: string_value}. Raises
    RuntimeError if the query fails, TimeoutError if it doesn't finish
    within timeout_seconds.
    """
    client = boto3.client(
        "athena",
        region_name=AWS_REGION,
        aws_access_key_id=AWS_ACCESS_KEY_ID,
        aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        aws_session_token=AWS_SESSION_TOKEN,
    )

    start_kwargs = {"QueryString": query, "WorkGroup": ATHENA_WORKGROUP}
    if ATHENA_OUTPUT_S3:
        start_kwargs["ResultConfiguration"] = {"OutputLocation": ATHENA_OUTPUT_S3}

    query_execution_id = client.start_query_execution(**start_kwargs)["QueryExecutionId"]

    deadline = time.time() + timeout_seconds
    while True:
        status = client.get_query_execution(QueryExecutionId=query_execution_id)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            reason = status.get("StateChangeReason", "no reason given")
            raise RuntimeError(f"Athena query {state.lower()}: {reason}")
        if time.time() > deadline:
            raise TimeoutError(f"Athena query still {state} after {timeout_seconds}s")
        time.sleep(poll_interval)

    rows = []
    columns = None
    paginator = client.get_paginator("get_query_results")
    for page in paginator.paginate(QueryExecutionId=query_execution_id):
        result_rows = page["ResultSet"]["Rows"]
        if columns is None:
            columns = [cell.get("VarCharValue") for cell in result_rows[0]["Data"]]
            result_rows = result_rows[1:]
        for row in result_rows:
            values = [cell.get("VarCharValue") for cell in row["Data"]]
            rows.append(dict(zip(columns, values)))
    return rows
