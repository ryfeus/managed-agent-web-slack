#!/usr/bin/env python3
"""Reject untracked test skips, fixmes, and expected failures."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LIMITATIONS_PATH = Path("knowledge/known-limitations.json")
ACTIVE_LIMITATION_STATUSES = {"accepted", "open"}
EXCLUDED_DIRECTORIES = {
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
PYTHON_DISABLE_MECHANISMS = {
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "pytest.mark.xfail",
    "pytest.skip",
    "pytest.xfail",
    "unittest.expectedFailure",
    "unittest.skip",
    "unittest.skipIf",
    "unittest.skipUnless",
}
JAVASCRIPT_DISABLE_PATTERN = re.compile(
    r"\b(?P<mechanism>(?:test\.describe|test|describe|it)\.(?:skip|fixme)|xit|xdescribe)\s*\("
)
PERMANENT_EXCEPTIONS = {
    (
        "backend/tests/integration/test_postgres.py",
        "pytest.mark.skipif",
    ): "RUN_POSTGRES_TESTS",
    (
        "backend/tests/integration/test_real_services.py",
        "pytest.skip",
    ): "RUN_INTEGRATION_TESTS",
}


@dataclass(frozen=True)
class DisabledTest:
    path: Path
    line: int
    mechanism: str
    source: str

    def render(self, root: Path) -> str:
        return f"{self.path.relative_to(root)}:{self.line}"


def _is_excluded(path: Path, root: Path) -> bool:
    return any(part in EXCLUDED_DIRECTORIES for part in path.relative_to(root).parts)


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".", 1)[0] in {"pytest", "unittest"}:
                    aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"pytest", "unittest"}:
            for item in node.names:
                aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _normalize_name(name: str | None, aliases: dict[str, str]) -> str | None:
    if name is None:
        return None
    first, separator, remainder = name.partition(".")
    replacement = aliases.get(first)
    if replacement is None:
        return name
    return replacement + (separator + remainder if separator else "")


def _python_disabled_tests(path: Path) -> tuple[list[DisabledTest], str | None]:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as error:
        return [], f"{path}:{error.lineno or 1}: cannot inspect invalid Python: {error.msg}"
    aliases = _import_aliases(tree)
    found: dict[tuple[int, str], DisabledTest] = {}

    def record(node: ast.AST, expression: ast.AST) -> None:
        mechanism = _normalize_name(_dotted_name(expression), aliases)
        if mechanism not in PYTHON_DISABLE_MECHANISMS:
            return
        line = getattr(node, "lineno", 1)
        segment = ast.get_source_segment(source, node) or ""
        found[(line, mechanism)] = DisabledTest(path, line, mechanism, segment)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            record(node, node.func)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    record(decorator, decorator)
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is not None and not isinstance(value, ast.Call):
                record(value, value)
    return sorted(found.values(), key=lambda item: (item.line, item.mechanism)), None


def _mask_javascript_non_code(source: str) -> str:
    chars = list(source)
    state = "normal"
    quote = ""
    index = 0
    while index < len(chars):
        char = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "normal":
            if char in {"'", '"', "`"}:
                quote = char
                chars[index] = " "
                state = "string"
            elif char == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "line_comment"
            elif char == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "block_comment"
        elif state == "string":
            if char == "\\":
                chars[index] = " "
                if index + 1 < len(chars):
                    if chars[index + 1] != "\n":
                        chars[index + 1] = " "
                    index += 1
            elif char == quote:
                chars[index] = " "
                state = "normal"
            elif char != "\n":
                chars[index] = " "
        elif state == "line_comment":
            if char == "\n":
                state = "normal"
            else:
                chars[index] = " "
        elif state == "block_comment":
            if char == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "normal"
            elif char != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def _javascript_disabled_tests(path: Path) -> list[DisabledTest]:
    source = path.read_text(encoding="utf-8")
    masked = _mask_javascript_non_code(source)
    return [
        DisabledTest(
            path,
            masked.count("\n", 0, match.start()) + 1,
            match.group("mechanism"),
            source[match.start() : match.end()],
        )
        for match in JAVASCRIPT_DISABLE_PATTERN.finditer(masked)
    ]


def _load_active_limitations(root: Path) -> tuple[dict[str, str], list[str]]:
    path = root / LIMITATIONS_PATH
    if not path.is_file():
        return {}, [f"{LIMITATIONS_PATH}: file is missing"]
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, [f"{LIMITATIONS_PATH}: invalid JSON: {error}"]
    if not isinstance(data, list):
        return {}, [f"{LIMITATIONS_PATH}: top-level value must be an array"]
    active: dict[str, str] = {}
    errors: list[str] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            errors.append(f"{LIMITATIONS_PATH}[{index}]: entry must be an object")
            continue
        limitation_id = item.get("id")
        status = item.get("status")
        test = item.get("test")
        if (
            isinstance(limitation_id, str)
            and re.fullmatch(r"KL-\d{3}", limitation_id)
            and status in ACTIVE_LIMITATION_STATUSES
            and isinstance(test, str)
        ):
            active[limitation_id] = Path(test).as_posix()
    return active, errors


def _test_files(root: Path) -> tuple[list[Path], list[Path]]:
    python: list[Path] = []
    javascript: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or _is_excluded(path, root):
            continue
        relative = path.relative_to(root)
        is_test_path = "tests" in relative.parts or path.name.startswith("test_")
        if path.suffix == ".py" and is_test_path:
            python.append(path)
        elif path.suffix in {".js", ".jsx", ".ts", ".tsx"} and (
            "e2e" in relative.parts or ".test." in path.name or ".spec." in path.name
        ):
            javascript.append(path)
    return sorted(python), sorted(javascript)


def _has_active_limitation(path: Path, root: Path, active: dict[str, str]) -> bool:
    relative = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8")
    cited = set(re.findall(r"\bKL-\d{3}\b", source))
    return any(active.get(limitation_id) == relative for limitation_id in cited)


def _is_permanent_exception(disabled: DisabledTest, root: Path) -> bool:
    relative = disabled.path.relative_to(root).as_posix()
    required = PERMANENT_EXCEPTIONS.get((relative, disabled.mechanism))
    return required is not None and required in disabled.source


def check_repository(root: Path) -> list[str]:
    active, errors = _load_active_limitations(root)
    python_files, javascript_files = _test_files(root)
    disabled: list[DisabledTest] = []
    for path in python_files:
        found, parse_error = _python_disabled_tests(path)
        disabled.extend(found)
        if parse_error:
            errors.append(parse_error.replace(f"{root}/", ""))
    for path in javascript_files:
        disabled.extend(_javascript_disabled_tests(path))
    for item in disabled:
        if _is_permanent_exception(item, root) or _has_active_limitation(item.path, root, active):
            continue
        errors.append(
            f"{item.render(root)}: test disabling via {item.mechanism} requires an active "
            "known-limitation entry for this file or an exact permanent harness exception"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    errors = check_repository(root)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"test rail check failed with {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Test rail checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
