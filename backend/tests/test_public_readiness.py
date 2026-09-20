from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def load_checker():
    path = REPOSITORY_ROOT / "scripts" / "check_public_readiness.py"
    spec = importlib.util.spec_from_file_location("check_public_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_readiness_rejects_generated_material(tmp_path: Path) -> None:
    checker = load_checker()
    generated = tmp_path / ".generated" / "manifest.yaml"
    generated.parent.mkdir()
    generated.write_text("safe", encoding="utf-8")

    errors = checker.scan_paths(tmp_path, [Path(".generated/manifest.yaml")])

    assert any("generated deployment material" in error for error in errors)


def test_public_readiness_allows_regular_plans_and_synthetic_fixture(tmp_path: Path) -> None:
    checker = load_checker()
    plan = tmp_path / "plans" / "history.md"
    plan.parent.mkdir()
    plan.write_text("public planning note", encoding="utf-8")
    fixture = tmp_path / "backend" / "tests" / "fixture.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(next(iter(checker.ALLOWED_SYNTHETIC_CREDENTIALS)), encoding="utf-8")

    assert checker.scan_paths(tmp_path, [Path("plans/history.md"), Path("backend/tests/fixture.py")]) == []


@pytest.mark.parametrize(
    "value",
    [
        "".join(("xoxb-", "123456789012-123456789012-abcdefghijklmnopqrstuv")),
        "".join(("xapp-", "1-ABCDEF-1234567890-abcdefghijklmnopqrstuvwxyz")),
        "".join(("xoxp-", "123456789012-123456789012-abcdefghijklmnopqrstuv")),
        "".join(("sk-ant-", "example0123456789abcdefghijklmnop")),
        "".join(("whsec_", "example0123456789abcdefghijklmnop")),
        "".join(("AKIA", "IOSFODNN7EXAMPLE")),
        "".join(("ASIA", "IOSFODNN7EXAMPLE")),
    ],
)
def test_public_readiness_rejects_realistic_credential_shapes(tmp_path: Path, value: str) -> None:
    checker = load_checker()
    source = tmp_path / "README.md"
    source.write_text(value, encoding="utf-8")

    assert any("credential-shaped" in error for error in checker.scan_paths(tmp_path, [Path("README.md")]))


def test_public_readiness_allows_only_the_environment_template(tmp_path: Path) -> None:
    checker = load_checker()
    template = tmp_path / ".env.example"
    local = tmp_path / ".env.production"
    template.write_text("AWS_REGION=us-west-2", encoding="utf-8")
    local.write_text("AWS_REGION=us-west-2", encoding="utf-8")

    errors = checker.scan_paths(tmp_path, [Path(".env.example"), Path(".env.production")])

    assert any("environment files" in error for error in errors)
    assert all(not error.startswith(".env.example:") for error in errors)
