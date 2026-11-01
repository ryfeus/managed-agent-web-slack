#!/usr/bin/env python3
"""Scan tracked and nonignored untracked files with pinned, standard Gitleaks rules."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "8.24.3"


def main() -> None:
    executable = shutil.which("gitleaks")
    if not executable:
        raise SystemExit(f"Install Gitleaks {VERSION}; see docs/testing.md.")
    version = subprocess.check_output([executable, "version"], text=True).strip()
    if version.removeprefix("v") != VERSION:
        raise SystemExit(f"Use Gitleaks {VERSION} for reproducible scans.")
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT
        )
        .decode()
        .split("\0")
    )
    with tempfile.TemporaryDirectory(prefix="repository-secret-scan-") as directory:
        for value in set(paths) - {""}:
            source = ROOT / value
            if not source.exists():
                continue  # a tracked deletion
            if source.is_symlink():
                raise SystemExit("Secret scan refuses symlinks: " + value)
            target = Path(directory) / value
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        report = Path(directory) / "gitleaks-report.json"
        result = subprocess.run(
            [
                executable,
                "dir",
                directory,
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                str(report),
            ]
        )
        if report.exists():
            for finding in json.loads(report.read_text()):
                path = Path(finding["File"]).relative_to(directory)
                print(f"{finding['RuleID']}: {path}:{finding['StartLine']}")
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
