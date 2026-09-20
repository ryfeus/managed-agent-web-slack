#!/usr/bin/env python3
"""Compare migration-owned tables and columns with the persistence contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path("knowledge/persistence-schema-contract.json")
MIGRATIONS_PATH = Path("backend/migrations")
IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
TABLE_CONSTRAINTS = {"check", "constraint", "exclude", "foreign", "primary", "unique"}


@dataclass(frozen=True)
class Location:
    path: Path
    line: int

    def render(self, root: Path) -> str:
        return f"{self.path.relative_to(root)}:{self.line}"


@dataclass
class Schema:
    tables: dict[str, dict[str, Location]]
    table_locations: dict[str, Location]


def _mask_sql(text: str) -> str:
    """Mask comments and string literals while preserving offsets and newlines."""
    chars = list(text)
    index = 0
    state = "normal"
    while index < len(chars):
        char = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "normal":
            if char == "'":
                chars[index] = " "
                state = "string"
            elif char == "-" and following == "-":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "line_comment"
            elif char == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                index += 1
                state = "block_comment"
        elif state == "string":
            if char == "'" and following == "'":
                chars[index] = chars[index + 1] = " "
                index += 1
            elif char == "'":
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


def _statements(text: str) -> list[tuple[str, int]]:
    masked = _mask_sql(text)
    statements: list[tuple[str, int]] = []
    start = 0
    for match in re.finditer(r";", masked):
        value = masked[start : match.start()]
        if value.strip():
            first = start + len(value) - len(value.lstrip())
            statements.append((value.strip(), text.count("\n", 0, first) + 1))
        start = match.end()
    tail = masked[start:]
    if tail.strip():
        first = start + len(tail) - len(tail.lstrip())
        statements.append((tail.strip(), text.count("\n", 0, first) + 1))
    return statements


def _split_top_level(value: str) -> list[tuple[str, int]]:
    parts: list[tuple[str, int]] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced closing parenthesis")
        elif char == "," and depth == 0:
            part = value[start:index]
            parts.append((part, value.count("\n", 0, start)))
            start = index + 1
    if depth != 0:
        raise ValueError("unbalanced parentheses")
    parts.append((value[start:], value.count("\n", 0, start)))
    return parts


def _create_table(statement: str, location: Location, schema: Schema, root: Path) -> list[str] | None:
    match = re.fullmatch(
        rf"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({IDENTIFIER})\s*\((.*)\)\s*",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    table = match.group(1).lower()
    if table in schema.tables:
        return [f"{location.render(root)}: duplicate table definition: {table}"]
    columns: dict[str, Location] = {}
    errors: list[str] = []
    try:
        definitions = _split_top_level(match.group(2))
    except ValueError as error:
        return [f"{location.render(root)}: cannot parse CREATE TABLE {table}: {error}"]
    body_line = location.line + statement[: match.start(2)].count("\n")
    for definition, line_offset in definitions:
        token = re.match(rf"\s*({IDENTIFIER})\b", definition)
        if token is None:
            errors.append(
                f"{location.path.relative_to(root)}:{body_line + line_offset}: "
                f"cannot parse table item in {table}"
            )
            continue
        column = token.group(1).lower()
        if column in TABLE_CONSTRAINTS:
            continue
        token_line_offset = definition.count("\n", 0, token.start(1))
        column_location = Location(location.path, body_line + line_offset + token_line_offset)
        if column in columns:
            errors.append(f"{column_location.render(root)}: duplicate column definition: {table}.{column}")
        else:
            columns[column] = column_location
    if not errors:
        schema.tables[table] = columns
        schema.table_locations[table] = location
    return errors


def _alter_table(statement: str, location: Location, schema: Schema, root: Path) -> list[str] | None:
    match = re.fullmatch(
        rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?({IDENTIFIER})\s+(.+)",
        statement,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return None
    table = match.group(1).lower()
    action = match.group(2).strip()
    if table not in schema.tables:
        return [f"{location.render(root)}: ALTER TABLE references unknown table: {table}"]
    try:
        if len(_split_top_level(action)) != 1:
            return [
                f"{location.render(root)}: multiple ALTER TABLE actions are unsupported; "
                "update the contract parser"
            ]
    except ValueError as error:
        return [f"{location.render(root)}: cannot parse ALTER TABLE {table}: {error}"]

    add = re.fullmatch(
        rf"ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?({IDENTIFIER})\b.*",
        action,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if add:
        column = add.group(1).lower()
        if column in schema.tables[table]:
            return [f"{location.render(root)}: duplicate column definition: {table}.{column}"]
        schema.tables[table][column] = location
        return []

    drop = re.fullmatch(
        rf"DROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?({IDENTIFIER})(?:\s+CASCADE|\s+RESTRICT)?",
        action,
        flags=re.IGNORECASE,
    )
    if drop:
        column = drop.group(1).lower()
        if column not in schema.tables[table]:
            return [f"{location.render(root)}: DROP COLUMN references unknown column: {table}.{column}"]
        del schema.tables[table][column]
        return []

    rename_column = re.fullmatch(
        rf"RENAME\s+COLUMN\s+({IDENTIFIER})\s+TO\s+({IDENTIFIER})",
        action,
        flags=re.IGNORECASE,
    )
    if rename_column:
        old, new = (value.lower() for value in rename_column.groups())
        if old not in schema.tables[table]:
            return [f"{location.render(root)}: RENAME references unknown column: {table}.{old}"]
        if new in schema.tables[table]:
            return [f"{location.render(root)}: RENAME would duplicate column: {table}.{new}"]
        del schema.tables[table][old]
        schema.tables[table][new] = location
        return []

    rename_table = re.fullmatch(rf"RENAME\s+TO\s+({IDENTIFIER})", action, flags=re.IGNORECASE)
    if rename_table:
        new = rename_table.group(1).lower()
        if new in schema.tables:
            return [f"{location.render(root)}: RENAME would duplicate table: {new}"]
        schema.tables[new] = schema.tables.pop(table)
        schema.table_locations[new] = location
        del schema.table_locations[table]
        return []

    return [f"{location.render(root)}: unsupported ALTER TABLE form; update the contract parser"]


def _drop_table(statement: str, location: Location, schema: Schema, root: Path) -> list[str] | None:
    match = re.fullmatch(
        rf"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?({IDENTIFIER})(?:\s+CASCADE|\s+RESTRICT)?",
        statement,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    table = match.group(1).lower()
    if table not in schema.tables:
        return [f"{location.render(root)}: DROP TABLE references unknown table: {table}"]
    del schema.tables[table]
    del schema.table_locations[table]
    return []


def parse_migrations(root: Path) -> tuple[Schema, list[str]]:
    migrations = root / MIGRATIONS_PATH
    schema = Schema({}, {})
    if not migrations.is_dir():
        return schema, [f"{MIGRATIONS_PATH}: directory is missing"]
    paths = sorted(migrations.glob("*.sql"))
    if not paths:
        return schema, [f"{MIGRATIONS_PATH}: no migrations found"]
    errors: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for statement, line in _statements(text):
            location = Location(path, line)
            upper = statement.lstrip().upper()
            if upper.startswith("CREATE TABLE"):
                result = _create_table(statement, location, schema, root)
            elif upper.startswith("ALTER TABLE"):
                result = _alter_table(statement, location, schema, root)
            elif upper.startswith("DROP TABLE"):
                result = _drop_table(statement, location, schema, root)
            else:
                result = []
            if result is None:
                errors.append(
                    f"{location.render(root)}: unsupported table-changing statement; "
                    "update the contract parser"
                )
            else:
                errors.extend(result)
    return schema, errors


def load_contract(root: Path) -> tuple[dict[str, set[str]], list[str]]:
    path = root / CONTRACT_PATH
    if not path.is_file():
        return {}, [f"{CONTRACT_PATH}: file is missing"]
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {}, [f"{CONTRACT_PATH}: invalid JSON: {error}"]
    if not isinstance(data, dict) or set(data) != {"tables"} or not isinstance(data["tables"], dict):
        return {}, [f"{CONTRACT_PATH}: expected exactly one object field named 'tables'"]
    contract: dict[str, set[str]] = {}
    errors: list[str] = []
    for table, columns in data["tables"].items():
        if not isinstance(table, str) or not re.fullmatch(IDENTIFIER, table):
            errors.append(f"{CONTRACT_PATH}: invalid table name: {table!r}")
            continue
        if not isinstance(columns, list) or not columns:
            errors.append(f"{CONTRACT_PATH}: {table} must contain a non-empty column array")
            continue
        normalized: set[str] = set()
        for column in columns:
            if not isinstance(column, str) or not re.fullmatch(IDENTIFIER, column):
                errors.append(f"{CONTRACT_PATH}: invalid column name for {table}: {column!r}")
            elif column in normalized:
                errors.append(f"{CONTRACT_PATH}: duplicate contracted column: {table}.{column}")
            else:
                normalized.add(column)
        contract[table] = normalized
    return contract, errors


def compare(root: Path, schema: Schema, contract: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    for table in sorted(schema.tables.keys() - contract.keys()):
        errors.append(
            f"{schema.table_locations[table].render(root)}: uncontracted persistence table: {table}"
        )
    for table in sorted(contract.keys() - schema.tables.keys()):
        errors.append(f"{CONTRACT_PATH}: contracted table is missing from migrations: {table}")
    for table in sorted(schema.tables.keys() & contract.keys()):
        actual = schema.tables[table]
        for column in sorted(actual.keys() - contract[table]):
            errors.append(f"{actual[column].render(root)}: uncontracted persistence column: {table}.{column}")
        for column in sorted(contract[table] - actual.keys()):
            errors.append(f"{CONTRACT_PATH}: contracted column is missing from migrations: {table}.{column}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    schema, migration_errors = parse_migrations(root)
    contract, contract_errors = load_contract(root)
    errors = [*migration_errors, *contract_errors]
    if not errors:
        errors.extend(compare(root, schema, contract))
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"persistence contract check failed with {len(errors)} violation(s)", file=sys.stderr)
        return 1
    print("Persistence contract checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
