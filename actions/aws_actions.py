"""
actions/aws_actions.py
Vega — Phase 4: AWS Log Retrieval Layer

This module is READ-ONLY. No AWS write operations are performed here.
Safety gate is enforced at the server level via POST /action/confirm.

Provides CloudWatch Logs retrieval and Lambda metadata fetch for the
Log Parsing Agent and Incident Analysis Agent during Ops Mode investigation.

Primary SDK: Boto3 (Nova Act is secondary/fallback — not implemented here).
Credentials come exclusively from environment variables via python-dotenv.

Design rules:
- Boto3 first — all operations use the AWS SDK directly.
- Read-only — no PutLogEvents, no Lambda invocations, no ECS mutations.
- Pagination — all list/filter operations page through all results.
- Retry — get_cloudwatch_logs retries ONCE with a wider window if the
  initial query returns empty results (30-minute extension on start_time).
- ISO 8601 — datetime.fromisoformat() handles both 'Z' and '+00:00' suffixes.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")
_AWS_ACCESS_KEY_ID: Optional[str] = os.getenv("AWS_ACCESS_KEY_ID")
_AWS_SECRET_ACCESS_KEY: Optional[str] = os.getenv("AWS_SECRET_ACCESS_KEY")

# Retry window extension when an initial log query returns no events
_RETRY_WINDOW_MINUTES: int = 30


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class AWSActionError(Exception):
    """Raised when a Boto3 AWS operation fails in this module."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_logs_client():
    """Return a boto3 CloudWatch Logs client using env credentials."""
    return boto3.client(
        "logs",
        region_name=AWS_REGION,
        aws_access_key_id=_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_AWS_SECRET_ACCESS_KEY,
    )


def _get_lambda_client():
    """Return a boto3 Lambda client using env credentials."""
    return boto3.client(
        "lambda",
        region_name=AWS_REGION,
        aws_access_key_id=_AWS_ACCESS_KEY_ID,
        aws_secret_access_key=_AWS_SECRET_ACCESS_KEY,
    )


def _iso_to_ms(iso_str: str) -> int:
    """
    Convert an ISO 8601 datetime string to a millisecond epoch integer.

    Handles both 'Z' (UTC) and '+00:00' offset suffixes, as well as
    timezone-naive strings (assumed UTC).

    Args:
        iso_str: ISO 8601 datetime string, e.g. "2026-02-25T10:00:00Z".

    Returns:
        Millisecond epoch timestamp as int.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    # Normalise 'Z' → '+00:00' so fromisoformat works on Python 3.10 and below
    normalised = iso_str.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalised)

    # Attach UTC if still naive after parsing
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * 1000)


def _fetch_log_events(
    client,
    log_group: str,
    start_ms: int,
    end_ms: int,
    filter_pattern: str,
    max_events: int = 10_000,
) -> list[dict]:
    """
    Paginate through CloudWatch Logs filter_log_events for the given window.

    Args:
        client:         Boto3 CloudWatch Logs client.
        log_group:      CloudWatch log group name.
        start_ms:       Start of query window, millisecond epoch.
        end_ms:         End of query window, millisecond epoch.
        filter_pattern: CloudWatch filter pattern string (empty = all events).
        max_events:     Maximum number of events to retrieve (default 10,000).
                        Prevents unbounded memory growth on large log groups.

    Returns:
        List of raw event dicts: {timestamp, message, logStreamName}.

    Raises:
        ClientError: Propagated as-is; callers wrap in AWSActionError.
    """
    kwargs: dict = {
        "logGroupName": log_group,
        "startTime": start_ms,
        "endTime": end_ms,
        "interleaved": True,
    }
    if filter_pattern:
        kwargs["filterPattern"] = filter_pattern

    events: list[dict] = []

    while True:
        response = client.filter_log_events(**kwargs)
        events.extend(response.get("events", []))
        if len(events) >= max_events:
            logger.warning(
                "Hit max_events limit (%d) for log group %r — truncating.",
                max_events, log_group,
            )
            events = events[:max_events]
            break
        next_token = response.get("nextToken")
        if not next_token:
            break
        kwargs["nextToken"] = next_token

    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_aws_connection() -> bool:
    """
    Verify that AWS CloudWatch Logs is reachable with the configured credentials.

    Performs a lightweight describe_log_groups call (limit=1).
    Used by the GET /health endpoint to surface credential / network issues early.

    Returns:
        True if the call succeeds, False on any exception.
    """
    try:
        client = _get_logs_client()
        client.describe_log_groups(limit=1)
        logger.debug("AWS connection check: OK (region=%s)", AWS_REGION)
        return True
    except Exception as exc:
        logger.warning("AWS connection check failed: %s", exc)
        return False


def list_log_groups(prefix: str = "") -> list[str]:
    """
    Return all CloudWatch log group names, optionally filtered by prefix.

    Paginates through all pages using NextToken until exhausted.

    Args:
        prefix: Optional log group name prefix filter (e.g. '/aws/lambda').
                Empty string returns all log groups.

    Returns:
        List of log group name strings.

    Raises:
        AWSActionError: If the AWS API call fails.
    """
    try:
        client = _get_logs_client()
        kwargs: dict = {}
        if prefix:
            kwargs["logGroupNamePrefix"] = prefix

        groups: list[str] = []

        while True:
            response = client.describe_log_groups(**kwargs)
            groups.extend(g["logGroupName"] for g in response.get("logGroups", []))
            next_token = response.get("nextToken")
            if not next_token:
                break
            kwargs["nextToken"] = next_token

        logger.debug("list_log_groups(prefix=%r) → %d groups", prefix, len(groups))
        return groups

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise AWSActionError(
            f"CloudWatch describe_log_groups failed ({code}): {exc}"
        ) from exc
    except Exception as exc:
        raise AWSActionError(f"Unexpected error in list_log_groups: {exc}") from exc


def get_cloudwatch_logs(
    log_group: str,
    start_time_iso: str,
    end_time_iso: str,
    filter_pattern: str = "",
) -> list[dict]:
    """
    Retrieve log events from a CloudWatch Logs group for the given time window.

    Paginates through all result pages. If the initial query returns no events,
    retries ONCE with the start time shifted back by 30 minutes. A warning is
    logged when the retry is triggered.

    Args:
        log_group:      CloudWatch log group name (e.g. '/aws/lambda/my-fn').
        start_time_iso: ISO 8601 start datetime (e.g. '2026-02-25T09:00:00Z').
        end_time_iso:   ISO 8601 end datetime   (e.g. '2026-02-25T10:00:00Z').
        filter_pattern: Optional CloudWatch filter pattern. Defaults to '' (all).

    Returns:
        List of event dicts, each containing:
            - timestamp     (int, ms epoch)
            - message       (str)
            - logStreamName (str)

    Raises:
        AWSActionError: If the AWS API call fails.
    """
    try:
        client = _get_logs_client()
        start_ms = _iso_to_ms(start_time_iso)
        end_ms = _iso_to_ms(end_time_iso)

        logger.info(
            "Fetching CloudWatch logs: group=%r start=%s end=%s pattern=%r",
            log_group, start_time_iso, end_time_iso, filter_pattern,
        )

        events = _fetch_log_events(client, log_group, start_ms, end_ms, filter_pattern)

        if not events:
            widened_start_ms = start_ms - (_RETRY_WINDOW_MINUTES * 60 * 1000)
            logger.warning(
                "No log events found for %r in [%s, %s]. "
                "Widening window by %d minutes and retrying once.",
                log_group, start_time_iso, end_time_iso, _RETRY_WINDOW_MINUTES,
            )
            events = _fetch_log_events(
                client, log_group, widened_start_ms, end_ms, filter_pattern
            )
            logger.info(
                "Retry returned %d event(s) for %r", len(events), log_group
            )

        logger.info(
            "get_cloudwatch_logs complete: %d event(s) from %r",
            len(events), log_group,
        )
        return events

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise AWSActionError(
            f"CloudWatch filter_log_events failed ({code}): {exc}"
        ) from exc
    except AWSActionError:
        raise
    except Exception as exc:
        raise AWSActionError(f"Unexpected error in get_cloudwatch_logs: {exc}") from exc


def get_lambda_logs(
    function_name: str,
    start_time_iso: str,
    end_time_iso: str,
) -> list[dict]:
    """
    Retrieve CloudWatch logs for an AWS Lambda function.

    Builds the standard Lambda log group path (/aws/lambda/{function_name})
    and delegates to get_cloudwatch_logs.

    Args:
        function_name:  Lambda function name (not ARN).
        start_time_iso: ISO 8601 start datetime.
        end_time_iso:   ISO 8601 end datetime.

    Returns:
        List of event dicts: {timestamp, message, logStreamName}.

    Raises:
        AWSActionError: If function_name is empty or the AWS call fails.
    """
    if not function_name or not function_name.strip():
        raise AWSActionError("function_name must not be empty.")

    log_group = f"/aws/lambda/{function_name.strip()}"
    logger.debug("get_lambda_logs: resolved log group → %r", log_group)
    return get_cloudwatch_logs(log_group, start_time_iso, end_time_iso)


def get_ecs_logs(
    cluster: str,
    service_name: str,
    start_time_iso: str,
    end_time_iso: str,
) -> list[dict]:
    """
    Retrieve CloudWatch logs for an ECS service.

    Builds the log group path as /ecs/{cluster}/{service_name} and delegates
    to get_cloudwatch_logs.

    Args:
        cluster:        ECS cluster name.
        service_name:   ECS service name.
        start_time_iso: ISO 8601 start datetime.
        end_time_iso:   ISO 8601 end datetime.

    Returns:
        List of event dicts: {timestamp, message, logStreamName}.

    Raises:
        AWSActionError: If cluster or service_name is empty, or the AWS call fails.
    """
    if not cluster or not cluster.strip():
        raise AWSActionError("cluster must not be empty.")
    if not service_name or not service_name.strip():
        raise AWSActionError("service_name must not be empty.")

    log_group = f"/ecs/{cluster.strip()}/{service_name.strip()}"
    logger.debug("get_ecs_logs: resolved log group → %r", log_group)
    return get_cloudwatch_logs(log_group, start_time_iso, end_time_iso)


def get_lambda_config(function_name: str) -> dict:
    """
    Fetch configuration metadata for an AWS Lambda function.

    Calls Lambda GetFunctionConfiguration and returns a normalised subset
    of the response for use by the Incident Analysis Agent.

    Args:
        function_name: Lambda function name or qualified ARN.

    Returns:
        Dict with keys:
            - function_name  (str)
            - runtime        (str, e.g. 'python3.12')
            - memory_size    (int, MB)
            - timeout        (int, seconds)
            - last_modified  (str, ISO 8601)
            - state          (str, e.g. 'Active' | 'Inactive' | 'Failed')

    Raises:
        AWSActionError: If function_name is empty or the AWS call fails.
    """
    if not function_name or not function_name.strip():
        raise AWSActionError("function_name must not be empty.")

    try:
        client = _get_lambda_client()
        logger.info("Fetching Lambda config for function: %r", function_name)
        response = client.get_function_configuration(FunctionName=function_name.strip())

        config = {
            "function_name": response.get("FunctionName", function_name),
            "runtime":       response.get("Runtime", "unknown"),
            "memory_size":   response.get("MemorySize", 0),
            "timeout":       response.get("Timeout", 0),
            "last_modified": response.get("LastModified", ""),
            "state":         response.get("State", "unknown"),
        }

        logger.debug("Lambda config for %r: %s", function_name, config)
        return config

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise AWSActionError(
            f"Lambda get_function_configuration failed ({code}): {exc}"
        ) from exc
    except Exception as exc:
        raise AWSActionError(f"Unexpected error in get_lambda_config: {exc}") from exc


# ---------------------------------------------------------------------------
# Smoke test (dev only — not pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("aws_actions.py smoke test")
    print("=" * 60)

    # 1. Connection check
    connected = check_aws_connection()
    print(f"\n[1] check_aws_connection() → {connected}")

    # 2. List log groups (first 3)
    try:
        groups = list_log_groups()
        preview = groups[:3]
        print(f"\n[2] list_log_groups() → {len(groups)} total group(s). First 3:")
        for g in preview:
            print(f"    {g}")
    except AWSActionError as exc:
        print(f"\n[2] list_log_groups() raised AWSActionError: {exc}", file=sys.stderr)

    print("\naws_actions.py smoke test passed")
