from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "scripts/check_persistence_contract.py"),
            "--root",
            str(root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def persistence_root(tmp_path: Path, sql: str, tables: dict[str, list[str]]) -> Path:
    (tmp_path / "backend/migrations").mkdir(parents=True)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "backend/migrations/001.sql").write_text(sql, encoding="utf-8")
    (tmp_path / "knowledge/persistence-schema-contract.json").write_text(
        json.dumps({"tables": tables}), encoding="utf-8"
    )
    return tmp_path


def test_persistence_contract_accepts_comments_literals_and_nested_constraints(tmp_path):
    root = persistence_root(
        tmp_path,
        """
        -- CREATE TABLE ignored (payload TEXT);
        CREATE TABLE safe (
            id TEXT PRIMARY KEY,
            status TEXT CHECK (status IN ('ready', 'message_body')),
            UNIQUE (id, status)
        );
        """,
        {"safe": ["id", "status"]},
    )
    result = run_checker(root)
    assert result.returncode == 0, result.stderr


def test_persistence_contract_applies_supported_alter_forms_in_order(tmp_path):
    root = persistence_root(
        tmp_path,
        """
        CREATE TABLE old_name (id TEXT, temporary TEXT);
        ALTER TABLE old_name ADD COLUMN state TEXT;
        ALTER TABLE old_name DROP COLUMN temporary;
        ALTER TABLE old_name RENAME COLUMN state TO status;
        ALTER TABLE old_name RENAME TO safe;
        """,
        {"safe": ["id", "status"]},
    )
    result = run_checker(root)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "sql, tables, expected",
    [
        ("CREATE TABLE surprise (id TEXT);", {}, "uncontracted persistence table: surprise"),
        (
            "CREATE TABLE safe (id TEXT, extra TEXT);",
            {"safe": ["id"]},
            "uncontracted persistence column: safe.extra",
        ),
        (
            "CREATE TABLE safe (id TEXT);",
            {"safe": ["id", "missing"]},
            "contracted column is missing from migrations: safe.missing",
        ),
        (
            "CREATE TABLE safe (id TEXT);",
            {"safe": ["id"], "missing": ["id"]},
            "contracted table is missing from migrations: missing",
        ),
        (
            "CREATE TABLE safe (id TEXT); CREATE TABLE safe (other TEXT);",
            {"safe": ["id"]},
            "duplicate table definition: safe",
        ),
        (
            "CREATE TABLE safe (id TEXT, id UUID);",
            {"safe": ["id"]},
            "duplicate column definition: safe.id",
        ),
        (
            "CREATE TABLE safe (id TEXT); ALTER TABLE safe ALTER COLUMN id TYPE UUID;",
            {"safe": ["id"]},
            "unsupported ALTER TABLE form",
        ),
    ],
)
def test_persistence_contract_rejects_schema_drift(tmp_path, sql, tables, expected):
    result = run_checker(persistence_root(tmp_path, sql, tables))
    assert result.returncode == 1
    assert expected in result.stderr
    assert ".sql:" in result.stderr or "persistence-schema-contract.json" in result.stderr
