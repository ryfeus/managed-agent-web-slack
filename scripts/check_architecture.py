#!/usr/bin/env python3
"""Enforce provider, composition, dependency, and persistence boundaries."""

from __future__ import annotations

import argparse
import ast
import os
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
            "backend/src/managed_agents_app/cma_controller/pending_input_s3.py",
            "backend/src/managed_agents_app/cma_controller/trigger_sqs.py",
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
            "backend/src/managed_agents_app/cma_controller/webhook.py",
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
CONTROLLER_FORBIDDEN_DEPENDENCIES = (
    "managed_agents_app.handlers",
    "managed_agents_app.slack",
    "managed_agents_app.agui_bridge",
    "managed_agents_app.agent_control_plane",
    "managed_agents_app.a2a_client",
    "managed_agents_app.db.thread_repository",
    "managed_agents_app.db.repositories",
    "managed_agents_app.testing",
)
A2A_APPLICATION_FORBIDDEN_DEPENDENCIES = (
    "managed_agents_app.cma_controller",
    "managed_agents_app.managed_agent",
    "managed_agents_app.slack",
)
SURFACE_FORBIDDEN_DEPENDENCIES = (
    "managed_agents_app.cma_controller",
    "managed_agents_app.managed_agent",
    "managed_agents_app.ports.agent",
)
LEGACY_PRODUCTION_PATTERNS = (
    re.compile(r"\bagent_sessions\b"),
    re.compile(r"\bsurface_bindings\b"),
    re.compile(r"\bManagedAgentSessionChanged\b"),
    re.compile(r"\bregister_and_bind\b"),
    re.compile(r"\bclaim_stream\b"),
    re.compile(r"/api/sessions\b"),
    re.compile(r"\bmanaged_event_id\b"),
)
FORBIDDEN_FRONTEND_PATTERNS = (
    re.compile(r"/api/sessions\b"),
    re.compile(r"\bsesn_"),
    re.compile(r"\btool_use_id\b"),
    re.compile(r"\bmanaged_event_id\b"),
    re.compile(r"\bsession\.status_"),
)
WEB_API_FORBIDDEN_DEPENDENCIES = (
    "managed_agents_app.managed_agent",
    "managed_agents_app.cma_controller",
    "managed_agents_app.ports.agent",
    "managed_agents_app.slack",
    "managed_agents_app.agui_bridge",
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
        ".terraform",
        ".generated",
        ".uv-cache",
        ".uv-python",
        ".venv",
        "__pycache__",
        "dist",
        "node_modules",
        "out",
        "test-results",
        "playwright-report",
        "playwright-report-production",
    }
)
FORBIDDEN_MIGRATION_FIELDS = {
    "assistant_message",
    "assistant_text",
    "chain_of_thought",
    "conversation_history",
    "message_body",
    "message_text",
    "messages_json",
    "history_json",
    "reasoning",
    "task_json",
    "tool_output_body",
    "transcript",
    "user_message",
    "user_text",
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


def _controller_imports(node: ast.Import | ast.ImportFrom, package: tuple[str, ...]) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        if node.level > len(package):
            return []
        base = ".".join((*package[: len(package) - node.level + 1], *(node.module or "").split(".")))
        base = base.rstrip(".")
    else:
        base = node.module or ""
    return [base, *(f"{base}.{alias.name}" for alias in node.names)]


def _forbidden_controller_dependency(
    node: ast.Import | ast.ImportFrom, package: tuple[str, ...]
) -> str | None:
    for module in _controller_imports(node, package):
        for prefix in CONTROLLER_FORBIDDEN_DEPENDENCIES:
            if module == prefix or module.startswith(f"{prefix}."):
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
    controllers = root / "backend/src/managed_agents_app/cma_controller"
    a2a_application_roots = (
        root / "backend/src/managed_agents_app/a2a_client",
        root / "backend/src/managed_agents_app/agent_control_plane",
        root / "backend/src/managed_agents_app/a2a_event_sink",
        root / "backend/src/managed_agents_app/agui_bridge",
    )
    is_handler = path.is_relative_to(handlers)
    is_port = path.is_relative_to(ports)
    is_controller = path.is_relative_to(controllers)
    is_a2a_application = any(path.is_relative_to(item) for item in a2a_application_roots)
    is_web_api = relative == "backend/src/managed_agents_app/handlers/web_api.py"
    is_slack_execution = path.is_relative_to(root / "backend/src/managed_agents_app/slack") or relative in {
        "backend/src/managed_agents_app/handlers/agent_input.py",
        "backend/src/managed_agents_app/handlers/slack_projector.py",
    }
    is_neutral = relative in NEUTRAL_MODULES
    is_surface = path.is_relative_to(root / "backend/src/managed_agents_app/slack") or is_handler
    violations: list[Violation] = []

    for node in ast.walk(parsed):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if is_surface:
                package = path.parent.relative_to(root / "backend/src").parts
                for module in _controller_imports(node, package):
                    for prefix in SURFACE_FORBIDDEN_DEPENDENCIES:
                        if module == prefix or module.startswith(f"{prefix}."):
                            violations.append(
                                Violation(path, node.lineno, f"surface code may not import '{prefix}'")
                            )
            if is_slack_execution:
                package = path.parent.relative_to(root / "backend/src").parts
                for module in _controller_imports(node, package):
                    for forbidden in (
                        "managed_agents_app.managed_agent",
                        "managed_agents_app.cma_controller",
                        "managed_agents_app.ports.agent",
                    ):
                        if module == forbidden or module.startswith(f"{forbidden}."):
                            violations.append(
                                Violation(
                                    path,
                                    node.lineno,
                                    f"Slack code may not import '{forbidden}'",
                                )
                            )
            if is_a2a_application:
                package = path.parent.relative_to(root / "backend/src").parts
                for module in _controller_imports(node, package):
                    for prefix in A2A_APPLICATION_FORBIDDEN_DEPENDENCIES:
                        if module == prefix or module.startswith(f"{prefix}."):
                            violations.append(
                                Violation(
                                    path,
                                    node.lineno,
                                    f"A2A application code may not import '{prefix}'",
                                )
                            )
            if is_web_api:
                package = path.parent.relative_to(root / "backend/src").parts
                for module in _controller_imports(node, package):
                    for prefix in WEB_API_FORBIDDEN_DEPENDENCIES:
                        if module == prefix or module.startswith(f"{prefix}."):
                            violations.append(
                                Violation(path, node.lineno, f"Web API may not import '{prefix}'")
                            )
            if is_controller:
                package = path.parent.relative_to(root / "backend/src").parts
                controller_dependency = _forbidden_controller_dependency(node, package)
                if controller_dependency is not None:
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            f"CMA controller may not import '{controller_dependency}'; "
                            "keep application and surface identity outside the controller",
                        )
                    )
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
        elif is_slack_execution and isinstance(node, ast.Attribute) and node.attr == "agent":
            if isinstance(node.value, ast.Name) and node.value.id == "runtime":
                violations.append(Violation(path, node.lineno, "Slack code may not access Runtime.agent"))
        elif (is_web_api or is_a2a_application) and isinstance(node, ast.Attribute) and node.attr == "agent":
            if isinstance(node.value, ast.Name) and node.value.id == "runtime":
                violations.append(Violation(path, node.lineno, "Web/A2A code may not access Runtime.agent"))
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
        if (
            path.is_relative_to(root / "backend/src/managed_agents_app")
            and isinstance(node, ast.Attribute)
            and node.attr == "agent"
            and isinstance(node.value, ast.Name)
            and node.value.id == "runtime"
        ):
            violations.append(Violation(path, node.lineno, "Runtime.agent is forbidden in production code"))
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
    return _source_files(root, {".py"})


def _source_files(root: Path, suffixes: set[str]) -> list[Path]:
    found: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in EXCLUDED_PYTHON_DIRECTORIES]
        found.extend(Path(directory) / name for name in files if Path(name).suffix in suffixes)
    return sorted(found)


def check_repository(root: Path) -> list[Violation]:
    violations: list[Violation] = []
    handlers = root / "backend/src/managed_agents_app/handlers"
    migrations = root / "backend/migrations"
    if not handlers.is_dir():
        violations.append(Violation(handlers, 1, "handler directory is missing"))
    for path in _python_files(root):
        violations.extend(check_python_file(path, root))
        if path.is_relative_to(root / "backend/src/managed_agents_app"):
            violations.extend(_check_active_text(path, root, LEGACY_PRODUCTION_PATTERNS))
    frontend = root / "apps/web"
    if frontend.is_dir():
        for path in _source_files(frontend, {".ts", ".tsx", ".js", ".jsx"}):
            violations.extend(_check_active_text(path, root, FORBIDDEN_FRONTEND_PATTERNS))
    if not migrations.is_dir():
        violations.append(Violation(migrations, 1, "migration directory is missing"))
    else:
        for path in sorted(migrations.rglob("*.sql")):
            violations.extend(check_migration(path))
    return violations


def _check_active_text(path: Path, root: Path, patterns: tuple[re.Pattern[str], ...]) -> list[Violation]:
    violations: list[Violation] = []
    for line, value in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for pattern in patterns:
            if pattern.search(value):
                violations.append(
                    Violation(path, line, f"active code contains forbidden legacy token: {pattern.pattern}")
                )
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
        print(
            f"architecture check failed with {len(violations)} violation(s)",
            file=sys.stderr,
        )
        return 1
    print("Architecture checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
