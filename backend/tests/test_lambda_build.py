from __future__ import annotations

import io
import json
import sys
import tarfile
from contextlib import nullcontext
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_lambda_build_context_excludes_secrets_and_preserves_cache_boundaries(tmp_path, monkeypatch):
    import build_lambdas

    checkout = tmp_path
    (checkout / "backend").mkdir()

    monkeypatch.setattr(build_lambdas, "ROOT", checkout)
    monkeypatch.setattr(build_lambdas, "lock", lambda: nullcontext())
    source = checkout / "backend/src/managed_agents_app"
    source.mkdir(parents=True)
    (source / "app.py").write_text("source")
    (source / "__pycache__").mkdir()
    (source / "__pycache__/app.pyc").write_bytes(b"bytecode")
    (checkout / ".env").write_text("secret")
    (checkout / "scripts").mkdir()
    (checkout / "scripts/Dockerfile.lambdas").write_text((SCRIPTS / "Dockerfile.lambdas").read_text())
    for name in ("run_agui_bridge.sh", "run_cma_controller.sh", "run_a2a_event_sink.sh"):
        (checkout / "backend" / name).write_text("launcher")
    (checkout / "services/openappa/target").mkdir(parents=True)
    (checkout / "services/openappa/Cargo.toml").write_text("fixture")
    commands = []

    def run(command):
        commands.append(command)
        if command[0] == "uv":
            (checkout / "dist/python-requirements.txt").write_text("locked-requirements")
        if command[:3] == ["docker", "buildx", "build"]:
            context = Path(command[-1])
            assert sorted(path.name for path in context.iterdir()) == [
                "Dockerfile",
                "launchers",
                "managed_agents_app",
                "openappa",
                "requirements.txt",
            ]
            assert not (context / "managed_agents_app/__pycache__").exists()
            assert not (context / "openappa/target").exists()
            assert "RUST_BUILD_IMAGE=image@sha256:fixture" in command
            assert (context / "requirements.txt").read_text() == "locked-requirements"
            assert "--no-cache" in command
            assert "BASE_IMAGE=image@sha256:fixture" in command
            output = command[command.index("--output") + 1]
            assert output.startswith("type=tar,dest=")
            with tarfile.open(output.split("dest=", 1)[1], "w") as archive:
                member = tarfile.TarInfo("lambdas/test.txt")
                member.size = 4
                archive.addfile(member, io.BytesIO(b"test"))
            dockerfile = (context / "Dockerfile").read_text()
            assert (
                dockerfile.index("COPY requirements.txt")
                < dockerfile.index("pip install")
                < dockerfile.index("COPY managed_agents_app")
            )
            assert "--mount=type=cache" in dockerfile

    monkeypatch.setattr(build_lambdas, "run", run)
    monkeypatch.setattr(
        build_lambdas, "capture", lambda *_: json.dumps([{"RepoDigests": ["image@sha256:fixture"]}])
    )
    monkeypatch.setenv("LAMBDA_BUILD_IMAGE", "image:tag")
    monkeypatch.setattr(sys, "argv", ["build_lambdas.py", "--no-cache"])
    build_lambdas.main()
    assert commands[-1] == ["bash", "scripts/validate_lambda_artifacts.sh"]
