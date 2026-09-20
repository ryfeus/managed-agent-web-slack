#!/usr/bin/env python3
"""Enforce provider, composition, dependency, and persistence boundaries."""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PROVIDER_SDK_ALLOWLIST: dict[str, frozenset[str]] = {
    "boto3": frozenset(
        {
            "backend/src/managed_agents_app/config.py",
            "backend/src/managed_agents_app/events.py",
            "backend/src/managed_agents_app/operations.py",
        }
    ),
    "botocore": frozenset({"backend/src/managed_agents_app/config.py"}),
    "slack_sdk": frozenset(
        {
            "backend/src/managed_agents_app/slack/client.py",
            "backend/src/managed_agents_app/slack/signatures.py",
            "backend/tests/test_retry_and_blocks.py",
        }
    ),
    "anthropic": frozenset(
        {
            "backend/src/managed_agents_app/managed_agent/client.py",
            # Narrow exception: this handler verifies Anthropic webhook signatures.
            # It does not construct or invoke the Managed Agent execution client.
            "backend/src/managed_agents_app/handlers/anthropic_webhook.py",
        }
    ),
}
PROVIDER_SDKS = frozenset(PROVIDER_SDK_ALLOWLIST)
FORBIDDEN_HANDLER_TYPES = {"ManagedAgentClient", "SlackClient", "WebClient"}
FORBIDDEN_PROVIDER_DEPENDENCIES = (
    "managed_agents_app.managed_agent",
    "managed_agents_app.slack",
    "managed_agents_app.testing",
)
NEUTRAL_MODULES = frozenset(
    {
        "backend/src/managed_agents_app/domain.py",
        "backend/src/managed_agents_app/event_wire.py",
        "backend/src/managed_agents_app/models.py",
    }
)
EXCLUDED_PYTHON_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "dist",
        "node_modules",
        "out",
    }
)
FORBIDDEN_MIGRATION_FIELDS = {
    "assistant_message",
    "chain_of_thought",
    "conversation_history",
    "message_body",
    "message_text",
    "reasoning",
    "tool_output_body",
    "transcript",
    "user_message",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def render(self, root: Path) -> str:
        return f"{self.path.relative_to(root)}:{self.line}: {self.message}"


def _provider_sdk(module: str) -> str | None:
    root = module.split(".", 1)[0]
    return root if root in PROVIDER_SDKS else None


def _imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module or ""]


def _forbidden_provider_dependency(module: str, level: int = 0) -> str | None:
    candidates = [module]
    if level and module:
        candidates.append(f"managed_agents_app.{module}")
    for candidate in candidates:
        for prefix in FORBIDDEN_PROVIDER_DEPENDENCIES:
            if candidate == prefix or candidate.startswith(f"{prefix}."):
                return prefix
    return None


def _parse_python(path: Path) -> ast.AST | Violation:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as error:
        line = error.lineno if isinstance(error, SyntaxError) else 1
        return Violation(path, line or 1, f"cannot check Python source: {error}")


def check_python_file(path: Path, root: Path) -> list[Violation]:
    parsed = _parse_python(path)
    if isinstance(parsed, Violation):
        return [parsed]

    relative = path.relative_to(root).as_posix()
    handlers = root / "backend/src/managed_agents_app/handlers"
    ports = root / "backend/src/managed_agents_app/ports"
    is_handler = path.is_relative_to(handlers)
    is_port = path.is_relative_to(ports)
    is_neutral = relative in NEUTRAL_MODULES
    violations: list[Violation] = []

    for node in ast.walk(parsed):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module in _imported_modules(node):
                sdk = _provider_sdk(module)
                if sdk is not None and relative not in PROVIDER_SDK_ALLOWLIST[sdk]:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            f"provider SDK import '{module}' is not allowed here; "
                            f"use an approved adapter or Runtime port (allowed paths for {sdk}: "
                            f"{', '.join(sorted(PROVIDER_SDK_ALLOWLIST[sdk]))})",
                        )
                    )

                dependency = _forbidden_provider_dependency(
                    module, node.level if isinstance(node, ast.ImportFrom) else 0
                )
                if dependency is not None and (is_port or is_neutral):
                    owner = "ports" if is_port else "neutral modules"
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            f"{owner} may not import provider implementation '{dependency}'; "
                            "depend on neutral models and protocols",
                        )
                    )

            if is_handler and isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in FORBIDDEN_HANDLER_TYPES:
                        violations.append(
                            Violation(
                                path,
                                node.lineno,
                                f"handlers may not import {alias.name}; "
                                "production composition belongs in Runtime",
                            )
                        )
        elif is_handler and isinstance(node, ast.Call):
            called: str | None = None
            if isinstance(node.func, ast.Name):
                called = node.func.id
            elif isinstance(node.func, ast.Attribute):
                called = node.func.attr
            if called in FORBIDDEN_HANDLER_TYPES:
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        f"handlers may not construct {called}; request it from Runtime",
                    )
                )
    return violations


def check_migration(path: Path) -> list[Violation]:
    violations: list[Violation] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.split("--", 1)[0]
        line = re.sub(r"'(?:''|[^'])*'", "", line)
        identifiers = {value.lower() for value in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", line)}
        for field in sorted(identifiers & FORBIDDEN_MIGRATION_FIELDS):
            violations.append(
                Violation(
                    path,
                    line_number,
                    f"migration field '{field}' may persist transcript or reasoning content; "
                    "Anthropic is canonical",
                )
            )
    return violations


def _python_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not any(part in EXCLUDED_PYTHON_DIRECTORIES for part in path.relative_to(root).parts)
    )


def check_repository(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    handlers = root / "backend/src/managed_agents_app/handlers"
    migrations = root / "backend/migrations"
    if not handlers.is_dir():
        violations.append(Violation(handlers, 1, "handler directory is missing"))
    for path in _python_files(root):
        violations.extend(check_python_file(path, root))
    if not migrations.is_dir():
        violations.append(Violation(migrations, 1, "migration directory is missing"))
    else:
        for path in sorted(migrations.rglob("*.sql")):
            violations.extend(check_migration(path))
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    violations = check_repository(root)
    for violation in violations:
        print(violation.render(root), file=sys.stderr)
    if violations:
        print(f"architecture check failed with {len(violations)} violation(s)", file=sys.stderr)
        return 1
    print("Architecture checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
