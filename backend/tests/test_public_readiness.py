from __future__ import annotations

import importlib.util
import json
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


def test_public_readiness_rejects_private_identifier_and_generated_material(tmp_path: Path) -> None:
    checker = load_checker()
    source = tmp_path / "README.md"
    source.write_text("".join(("339543", "757547")), encoding="utf-8")
    generated = tmp_path / ".generated" / "manifest.yaml"
    generated.parent.mkdir()
    generated.write_text("safe", encoding="utf-8")

    errors = checker.scan_paths(tmp_path, [Path("README.md"), Path(".generated/manifest.yaml")])

    assert any("private deployment identifier" in error for error in errors)
    assert any("generated deployment material" in error for error in errors)


def test_public_readiness_allows_private_plans_and_synthetic_fixture(tmp_path: Path) -> None:
    checker = load_checker()
    plan = tmp_path / "plans" / "history.md"
    plan.parent.mkdir()
    plan.write_text("".join(("339543", "757547")), encoding="utf-8")
    fixture = tmp_path / "backend" / "tests" / "fixture.py"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(next(iter(checker.FIXTURE_SECRETS)), encoding="utf-8")

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


def test_public_tree_mode_does_not_exempt_private_plans(tmp_path: Path) -> None:
    checker = load_checker()
    plan = tmp_path / "plans" / "history.md"
    plan.parent.mkdir()
    plan.write_text("".join(("339543", "757547")), encoding="utf-8")

    assert checker.scan_paths(tmp_path, [Path("plans/history.md")]) == []
    assert any(
        "private deployment identifier" in error
        for error in checker.scan_paths(tmp_path, [Path("plans/history.md")], allow_private_paths=False)
    )


def test_public_readiness_allows_only_the_environment_template(tmp_path: Path) -> None:
    checker = load_checker()
    template = tmp_path / ".env.example"
    local = tmp_path / ".env.production"
    template.write_text("AWS_REGION=us-west-2", encoding="utf-8")
    local.write_text("AWS_REGION=us-west-2", encoding="utf-8")

    errors = checker.scan_paths(tmp_path, [Path(".env.example"), Path(".env.production")])

    assert any("environment files" in error for error in errors)
    assert all(not error.startswith(".env.example:") for error in errors)


def test_lock_resource_identity_is_narrowly_allowed(tmp_path: Path) -> None:
    checker = load_checker()
    lock = tmp_path / "cma/claude-lock.json"
    lock.parent.mkdir()
    identities = [v for v in checker.FORBIDDEN_LITERALS if v.startswith(("agent_", "env_"))]
    data = {
        "resources": {
            "./agents/application.md": {"kind": "agent", "id": identities[0]},
            "./environments/application.yaml": {"kind": "environment", "id": identities[1]},
        }
    }
    lock.write_text(json.dumps(data))
    assert checker.scan_paths(tmp_path, [Path("cma/claude-lock.json")]) == []
    # Same values outside the two identity fields are still forbidden.
    data["note"] = identities[0]
    lock.write_text(json.dumps(data))
    assert checker.scan_paths(tmp_path, [Path("cma/claude-lock.json")])
    data.pop("note")
    data["secret"] = "sk-ant-" + "x" * 25
    lock.write_text(json.dumps(data))
    assert any("credential-shaped" in e for e in checker.scan_paths(tmp_path, [Path("cma/claude-lock.json")]))


def test_lock_exception_does_not_allow_wrong_resource_or_other_files(tmp_path: Path) -> None:
    checker = load_checker()
    identity = next(v for v in checker.FORBIDDEN_LITERALS if v.startswith("agent_"))
    path = tmp_path / "cma/claude-lock.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"resources": {"./agents/other.md": {"kind": "agent", "id": identity}}}))
    assert checker.scan_paths(tmp_path, [Path("cma/claude-lock.json")])
    readme = tmp_path / "README.md"
    readme.write_text(identity)
    assert checker.scan_paths(tmp_path, [Path("README.md")])
