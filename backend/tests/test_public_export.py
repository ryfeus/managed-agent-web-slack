from __future__ import annotations

import importlib.util
import json
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


def test_public_export_excludes_private_and_generated_material(tmp_path: Path) -> None:
    exporter = load_script("export_public_tree.py")
    source = tmp_path / "source"
    source.mkdir()
    for relative, content in {
        "README.md": "public",
        "LICENSE": "license",
        ".env.example": "AWS_REGION=us-west-2",
        "backend/tests/test_example.py": "test source",
        "plans/private.md": "private",
        "backend/src/__pycache__/module.pyc": "bytecode",
        ".generated/value.txt": "generated",
        ".env.local": "local",
        "test-results/result.txt": "artifact",
    }.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    exporter.tracked_paths = lambda root: [
        path.relative_to(source) for path in source.rglob("*") if path.is_file()
    ]
    output = tmp_path / "public"
    exported = exporter.export_tree(source, output)

    assert Path("README.md") in exported
    assert Path(".env.example") in exported
    assert Path("backend/tests/test_example.py") in exported
    assert not (output / "plans").exists()
    assert not (output / ".generated").exists()
    assert not (output / "backend/src/__pycache__").exists()
    assert not (output / "test-results").exists()
    assert not (output / ".env.local").exists()
    manifest = json.loads((output / exporter.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["files"]] == [path.as_posix() for path in exported]

    second_output = tmp_path / "public-second"
    exporter.export_tree(source, second_output)
    assert (output / exporter.MANIFEST_NAME).read_bytes() == (
        second_output / exporter.MANIFEST_NAME
    ).read_bytes()


def test_public_export_rejects_unsafe_or_nonempty_destinations(tmp_path: Path) -> None:
    exporter = load_script("export_public_tree.py")
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("public", encoding="utf-8")
    exporter.tracked_paths = lambda root: [Path("README.md")]
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("content", encoding="utf-8")

    with pytest.raises(ValueError, match="empty"):
        exporter.export_tree(source, occupied)
    with pytest.raises(ValueError, match="outside"):
        exporter.export_tree(source, source / "public")


def test_exported_tree_passes_strict_scan_and_detects_injected_secret(tmp_path: Path) -> None:
    exporter = load_script("export_public_tree.py")
    checker = load_script("check_public_readiness.py")
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("public", encoding="utf-8")
    exporter.tracked_paths = lambda root: [Path("README.md")]
    output = tmp_path / "public"
    exporter.export_tree(source, output)

    assert checker.scan_paths(output, checker.public_tree_paths(output), allow_private_paths=False) == []
    (output / "README.md").write_text(
        "".join(("xoxb-", "123456789012-123456789012-abcdefghijklmnopqrstuv")), encoding="utf-8"
    )
    assert any(
        "credential-shaped" in error
        for error in checker.scan_paths(output, checker.public_tree_paths(output), allow_private_paths=False)
    )
