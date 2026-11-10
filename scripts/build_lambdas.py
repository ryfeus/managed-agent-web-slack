#!/usr/bin/env python3
"""Build locked Linux Lambda dependencies in a separate BuildKit cache layer."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command):
    subprocess.run(command, cwd=ROOT, check=True)


def capture(command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=True).stdout


@contextlib.contextmanager
def lock():
    directory = ROOT / ".generated"
    directory.mkdir(exist_ok=True)
    with (directory / "lambda-build.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


os.environ["PYTHONDONTWRITEBYTECODE"] = "1"


def main():
    with lock():
        base = os.getenv("LAMBDA_BUILD_IMAGE", "public.ecr.aws/sam/build-python3.14:latest-x86_64")
        try:
            inspect = json.loads(capture(["docker", "image", "inspect", base]))[0]
        except subprocess.CalledProcessError:
            run(["docker", "pull", "--platform", "linux/amd64", base])
            inspect = json.loads(capture(["docker", "image", "inspect", base]))[0]
        # Resolve the installed image's digest without a network refresh on warm builds.
        # Runtime, architecture and digest participate in the dependency cache key.
        resolved = next(
            (
                digest
                for digest in inspect.get("RepoDigests", [])
                if digest.startswith(base.split(":")[0] + "@")
            ),
            None,
        )
        if not resolved:
            raise RuntimeError("Build image did not resolve to a repository digest.")
        rust_image = os.getenv("OPENAPPA_BUILD_IMAGE", "rust:1.99-alpine")
        try:
            rust_inspect = json.loads(capture(["docker", "image", "inspect", rust_image]))[0]
        except subprocess.CalledProcessError:
            run(["docker", "pull", "--platform", "linux/amd64", rust_image])
            rust_inspect = json.loads(capture(["docker", "image", "inspect", rust_image]))[0]
        rust_resolved = next(iter(rust_inspect.get("RepoDigests", [])), None)
        if not rust_resolved:
            raise RuntimeError("Rust build image did not resolve to a repository digest.")
        (ROOT / "dist").mkdir(exist_ok=True)
        run(
            [
                "uv",
                "export",
                "--quiet",
                "--directory",
                "backend",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                "../dist/python-requirements.txt",
            ]
        )
        cache_key = hashlib.sha256(
            (ROOT / "dist/python-requirements.txt").read_bytes()
            + resolved.encode()
            + b"python3.14-linux-amd64"
        ).hexdigest()
        run(["python3", "scripts/prepare_openappa.py"])
        # Construct a minimal context including only the verified native source.
        with tempfile.TemporaryDirectory(prefix="lambda-context-") as directory:
            context = Path(directory) / "context"
            context.mkdir()
            shutil.copy(ROOT / "dist/python-requirements.txt", context / "requirements.txt")
            shutil.copy(ROOT / "scripts/Dockerfile.lambdas", context / "Dockerfile")
            shutil.copytree(
                ROOT / "backend/src/managed_agents_app",
                context / "managed_agents_app",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            shutil.copytree(
                ROOT / "services/openappa", context / "openappa", ignore=shutil.ignore_patterns("target")
            )
            (context / "launchers").mkdir()
            for name in ("run_cma_controller.sh", "run_a2a_event_sink.sh"):
                shutil.copy(ROOT / "backend" / name, context / "launchers" / name)
            archive_path = Path(directory) / "artifacts.tar"
            run(
                [
                    "docker",
                    "buildx",
                    "build",
                    "--platform",
                    "linux/amd64",
                    "--build-arg",
                    "BASE_IMAGE=" + resolved,
                    "--build-arg",
                    "RUST_BUILD_IMAGE=" + rust_resolved,
                    "--build-arg",
                    "DEPENDENCY_CACHE_KEY=" + cache_key,
                    "--progress=plain",
                    "--output",
                    "type=tar,dest=" + str(archive_path),
                    *sys.argv[1:],
                    str(context),
                ]
            )
            for name in ("python-package", "lambdas"):
                shutil.rmtree(ROOT / "dist" / name, ignore_errors=True)
            # The tar exporter avoids per-file host transfers and publishes only
            # validated paths; no credential-bearing build context is mounted.
            with tarfile.open(archive_path) as archive:
                for member in archive.getmembers():
                    path = Path(member.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or (path.parts and path.parts[0] not in ("python-package", "lambdas"))
                    ):
                        raise RuntimeError("Unexpected build artifact path.")
                archive.extractall(ROOT / "dist", filter="data")
        run(["bash", "scripts/validate_lambda_artifacts.sh"])


if __name__ == "__main__":
    main()
