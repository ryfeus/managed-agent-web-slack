#!/usr/bin/env python3
"""Render an ignored Slack manifest from a public application URL."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPOSITORY_ROOT / "slack" / "manifest.template.yaml"
DEFAULT_OUTPUT = REPOSITORY_ROOT / ".generated" / "slack-manifest.yaml"


def normalize_public_app_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("PUBLIC_APP_URL must be an HTTPS origin without a path")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("PUBLIC_APP_URL must be an HTTPS origin without credentials, query, or fragment")
    return f"https://{parsed.netloc}", parsed.netloc


def render_manifest(template: str, public_app_url: str) -> str:
    origin, host = normalize_public_app_url(public_app_url)
    result = template.replace("__PUBLIC_APP_URL__", origin).replace("__PUBLIC_APP_HOST__", host)
    if "__PUBLIC_APP_" in result:
        raise ValueError("Slack manifest template contains an unresolved public application placeholder")
    return result


def terraform_application_url() -> str:
    return subprocess.check_output(
        ["terraform", "-chdir=infra/app", "output", "-raw", "application_url"],
        cwd=REPOSITORY_ROOT,
        text=True,
    ).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-app-url")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    public_app_url = args.public_app_url
    if not public_app_url:
        try:
            public_app_url = terraform_application_url()
        except subprocess.CalledProcessError:
            parser.error("provide --public-app-url or deploy Terraform before rendering the Slack manifest")
    try:
        content = render_manifest(TEMPLATE.read_text(encoding="utf-8"), public_app_url)
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"Rendered Slack manifest at {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
