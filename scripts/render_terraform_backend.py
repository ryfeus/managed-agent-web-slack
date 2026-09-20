#!/usr/bin/env python3
"""Render ignored Terraform S3 backend configuration for one deployment."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


def state_bucket_name(account_id: str, app_name: str, region: str, override: str | None = None) -> str:
    _validate_account_id(account_id)
    _validate_name("app name", app_name)
    _validate_region(region)
    if override is not None:
        bucket = override
    else:
        suffix = f"-terraform-state-{region}"
        bucket = f"{account_id}-{app_name}{suffix}"
        if len(bucket) > 63:
            digest = hashlib.sha256(app_name.encode("utf-8")).hexdigest()[:8]
            app_length = 63 - len(account_id) - len(suffix) - len(digest) - 2
            bucket = f"{account_id}-{app_name[:app_length]}-{digest}{suffix}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("state bucket must be a valid S3 bucket name")
    return bucket


def validate_expected_account(account_id: str, expected_account_id: str | None) -> None:
    _validate_account_id(account_id)
    if expected_account_id and account_id != expected_account_id:
        raise ValueError(f"Refusing to deploy to unexpected AWS account: {account_id}")


def render_backend(
    account_id: str, app_name: str, environment: str, region: str, state_bucket: str | None
) -> str:
    bucket = state_bucket_name(account_id, app_name, region, state_bucket)
    _validate_name("environment", environment)
    return (
        f'bucket       = "{bucket}"\n'
        f'key          = "{app_name}/{environment}/terraform.tfstate"\n'
        f'region       = "{region}"\n'
        "encrypt      = true\n"
        "use_lockfile = true\n"
    )


def _validate_account_id(account_id: str) -> None:
    if not re.fullmatch(r"\d{12}", account_id):
        raise ValueError("AWS account ID must contain exactly 12 digits")


def _validate_name(label: str, value: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ValueError(f"{label} must contain only lowercase letters, digits, and hyphens")


def _validate_region(region: str) -> None:
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-\d+", region):
        raise ValueError("AWS region is invalid")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--expected-account-id")
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--state-bucket")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        validate_expected_account(args.account_id, args.expected_account_id)
        content = render_backend(
            args.account_id, args.app_name, args.environment, args.region, args.state_bucket
        )
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Rendered Terraform backend configuration at {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
