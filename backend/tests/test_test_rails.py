from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def run_checker(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "scripts/check_test_rails.py"), "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def rails_root(tmp_path: Path, limitations: list[dict[str, str]] | None = None) -> Path:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "knowledge/known-limitations.json").write_text(
        json.dumps(limitations or []), encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize(
    "mechanism",
    [
        "test.skip",
        "test.fixme",
        "describe.skip",
        "describe.fixme",
        "test.describe.skip",
        "test.describe.fixme",
        "it.skip",
        "it.fixme",
        "xit",
        "xdescribe",
    ],
)
def test_test_rails_rejects_javascript_disable_primitives(tmp_path, mechanism):
    root = rails_root(tmp_path)
    path = root / "e2e/example.spec.ts"
    path.parent.mkdir()
    path.write_text(f"\n{mechanism}('disabled', () => {{}});\n", encoding="utf-8")
    result = run_checker(root)
    assert result.returncode == 1
    assert f"via {mechanism}" in result.stderr
    assert "example.spec.ts:2" in result.stderr


@pytest.mark.parametrize(
    "source, mechanism",
    [
        ("import pytest\npytest.skip('disabled')\n", "pytest.skip"),
        ("import pytest\npytest.xfail('disabled')\n", "pytest.xfail"),
        ("import pytest\npytestmark = pytest.mark.skip('disabled')\n", "pytest.mark.skip"),
        ("import pytest\npytestmark = pytest.mark.skip\n", "pytest.mark.skip"),
        ("import pytest\npytestmark = pytest.mark.skipif(True, reason='disabled')\n", "pytest.mark.skipif"),
        ("import pytest\npytestmark = pytest.mark.xfail(reason='disabled')\n", "pytest.mark.xfail"),
        ("import pytest\n@pytest.mark.xfail\ndef test_one(): pass\n", "pytest.mark.xfail"),
        ("import unittest\n@unittest.skip('disabled')\ndef test_one(): pass\n", "unittest.skip"),
        ("from unittest import skipIf\n@skipIf(True, 'disabled')\ndef test_one(): pass\n", "unittest.skipIf"),
        (
            "from unittest import skipUnless\n@skipUnless(False, 'disabled')\ndef test_one(): pass\n",
            "unittest.skipUnless",
        ),
        (
            "from unittest import expectedFailure\n@expectedFailure\ndef test_one(): pass\n",
            "unittest.expectedFailure",
        ),
    ],
)
def test_test_rails_rejects_python_disable_primitives(tmp_path, source, mechanism):
    root = rails_root(tmp_path)
    path = root / "backend/tests/test_example.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    result = run_checker(root)
    assert result.returncode == 1
    assert f"via {mechanism}" in result.stderr
    assert "test_example.py:" in result.stderr


def limitation(status: str = "open") -> dict[str, str]:
    return {
        "id": "KL-123",
        "title": "Example",
        "status": status,
        "owner": "tests",
        "test": "e2e/example.spec.ts",
        "desired_behavior": "The test will eventually run.",
    }


@pytest.mark.parametrize("status", ["open", "accepted"])
def test_test_rails_accepts_active_limitation_for_same_file(tmp_path, status):
    root = rails_root(tmp_path, [limitation(status)])
    path = root / "e2e/example.spec.ts"
    path.parent.mkdir()
    path.write_text("// KL-123\ntest.fixme('known gap', () => {});\n", encoding="utf-8")
    result = run_checker(root)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", ["resolved", "unknown"])
def test_test_rails_rejects_inactive_or_unknown_limitation(tmp_path, status):
    limitations = [limitation(status)] if status == "resolved" else []
    root = rails_root(tmp_path, limitations)
    path = root / "e2e/example.spec.ts"
    path.parent.mkdir()
    path.write_text("// KL-123\ntest.skip('gap', () => {});\n", encoding="utf-8")
    result = run_checker(root)
    assert result.returncode == 1
    assert "requires an active known-limitation" in result.stderr


@pytest.mark.parametrize(
    "relative_path, source",
    [
        (
            "backend/tests/integration/test_postgres.py",
            "import os\nimport pytest\npytestmark = pytest.mark.skipif("
            "os.getenv('RUN_POSTGRES_TESTS') != '1', reason='integration')\n",
        ),
        (
            "backend/tests/integration/test_real_services.py",
            "import pytest\ndef test_live():\n    "
            "pytest.skip('Set RUN_INTEGRATION_TESTS=1 for live services')\n",
        ),
    ],
)
def test_test_rails_accepts_exact_permanent_harness_exceptions(tmp_path, relative_path, source):
    root = rails_root(tmp_path)
    path = root / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    result = run_checker(root)
    assert result.returncode == 0, result.stderr


def test_test_rails_ignores_comments_strings_and_non_disabling_markers(tmp_path):
    root = rails_root(tmp_path)
    js = root / "e2e/example.spec.ts"
    js.parent.mkdir()
    js.write_text("// test.skip('comment')\nconst note = \"describe.fixme('string')\";\n")
    py = root / "backend/tests/test_example.py"
    py.parent.mkdir(parents=True)
    py.write_text("import pytest\npytestmark = pytest.mark.integration\nNOTE = 'pytest.skip()'\n")
    result = run_checker(root)
    assert result.returncode == 0, result.stderr
