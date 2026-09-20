from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = REPOSITORY_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backend_rendering_is_portable_and_deterministic() -> None:
    renderer = load_script("render_terraform_backend.py")
    content = renderer.render_backend("123456789012", "example-app", "dev", "us-west-2", None)

    assert 'bucket       = "123456789012-example-app-terraform-state-us-west-2"' in content
    assert 'key          = "example-app/dev/terraform.tfstate"' in content


def test_default_state_bucket_for_real_app_name_is_s3_valid() -> None:
    renderer = load_script("render_terraform_backend.py")

    bucket = renderer.state_bucket_name("123456789012", "managed-agent-web-slack", "us-west-2")

    assert len(bucket) <= 63
    assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
    assert bucket.endswith("-terraform-state-us-west-2")


def test_long_state_bucket_names_are_stable_and_distinct() -> None:
    renderer = load_script("render_terraform_backend.py")
    first = "a" * 59 + "-one"
    second = "a" * 59 + "-two"

    first_bucket = renderer.state_bucket_name("123456789012", first, "us-west-2")

    assert first_bucket == renderer.state_bucket_name("123456789012", first, "us-west-2")
    assert first_bucket != renderer.state_bucket_name("123456789012", second, "us-west-2")
    assert len(first_bucket) <= 63


@pytest.mark.parametrize("app_name", ["a", "a" * 63])
def test_state_bucket_accepts_minimum_and_maximum_app_names(app_name: str) -> None:
    renderer = load_script("render_terraform_backend.py")

    bucket = renderer.state_bucket_name("123456789012", app_name, "us-west-2")

    assert len(bucket) <= 63


def test_state_bucket_override_is_preserved() -> None:
    renderer = load_script("render_terraform_backend.py")

    assert (
        renderer.state_bucket_name(
            "123456789012", "managed-agent-web-slack", "us-west-2", "existing-state-bucket"
        )
        == "existing-state-bucket"
    )


def test_state_bucket_override_must_be_s3_valid() -> None:
    renderer = load_script("render_terraform_backend.py")

    with pytest.raises(ValueError, match="valid S3 bucket"):
        renderer.state_bucket_name("123456789012", "example-app", "us-west-2", "Invalid_Bucket")


def test_backend_rendering_rejects_unexpected_account() -> None:
    renderer = load_script("render_terraform_backend.py")

    with pytest.raises(ValueError, match="unexpected AWS account"):
        renderer.validate_expected_account("123456789012", "210987654321")


def test_slack_manifest_rendering_uses_deployment_origin() -> None:
    renderer = load_script("render_slack_manifest.py")
    template = (REPOSITORY_ROOT / "slack" / "manifest.template.yaml").read_text(encoding="utf-8")

    rendered = renderer.render_manifest(template, "https://example.cloudfront.net")

    assert "https://example.cloudfront.net/slack/events" in rendered
    assert "https://example.cloudfront.net/slack/interactions" in rendered
    assert "- example.cloudfront.net" in rendered
    assert "__PUBLIC_APP_" not in rendered


@pytest.mark.parametrize(
    "value", ["http://example.com", "https://example.com/path", "https://example.com?a=b"]
)
def test_slack_manifest_rejects_non_origin_urls(value: str) -> None:
    renderer = load_script("render_slack_manifest.py")

    with pytest.raises(ValueError):
        renderer.normalize_public_app_url(value)
