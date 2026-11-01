"""Acceptance of the small foreground runner's isolation and cleanup contract."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import local


def test_environment_is_fake_and_frontend_dotenv_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(local, "ROOT", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    monkeypatch.setenv("AWS_PROFILE", "must-not-inherit")
    monkeypatch.setenv("SLACK_USER_TOKEN", "must-not-inherit")
    (tmp_path / ".env").write_text("WEB_ACCESS_TOKEN=must-not-load\n")
    env = local.local_env({"frontend": 3000, "backend": 3001, "postgres": 54321})
    assert not {"ANTHROPIC_API_KEY", "AWS_PROFILE", "SLACK_USER_TOKEN"} & env.keys()
    assert "must-not" not in str(env)
    assert env["APP_ENV"] == "e2e" and env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "127.0.0.1" in env["DATABASE_URL"]
    monkeypatch.setattr(local.shutil, "which", lambda _: "/fixture/bin")
    (tmp_path / "node_modules/next").mkdir(parents=True)
    (tmp_path / "node_modules/next/package.json").write_text("{}")
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "apps/web/.env.local").write_text("NEXT_PUBLIC_API_BASE_URL=https://live.invalid")
    with pytest.raises(RuntimeError, match="Next would load live settings"):
        local.prerequisites()


def test_missing_prerequisite_has_actionable_failure(monkeypatch):
    monkeypatch.setattr(local.shutil, "which", lambda name: None if name == "docker" else "/fixture/bin")
    with pytest.raises(RuntimeError, match="Install missing prerequisites: docker"):
        local.prerequisites()


def test_interrupted_foreground_cleans_only_owned_children_and_compose(tmp_path):
    # Exercise real process groups and signals, without Docker, ports or app dependencies.
    code = f"""
import sys, time
from pathlib import Path
sys.path.insert(0, {str(local.ROOT / "scripts")!r})
import local
local.ROOT = Path({str(tmp_path)!r})
local.ports = lambda: {{"frontend": 1, "backend": 2, "postgres": 3}}
local.prerequisites = lambda: None
def start(self):
    self.spawn([sys.executable, "-c", "import time; time.sleep(60)"], "owned")
    self.compose_started = True
    print("CHILD " + str(self.children[0].pid), flush=True)
def run(self, command, **kwargs):
    import os, signal
    os.kill(os.getpid(), signal.SIGINT)  # npm's forwarded duplicate during cleanup
    (local.ROOT / "cleanup").write_text(" ".join(command))
local.Stack.start = start
local.Stack.run = run
raise SystemExit(local.main())
"""
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    process = subprocess.Popen([sys.executable, "-u", "-c", code, "dev"], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout
        assert process.stdout.readline().startswith("Logs:")
        child = int(process.stdout.readline().removeprefix("CHILD "))
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=15) == 130
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert unrelated.poll() is None
        cleanup = (tmp_path / "cleanup").read_text()
        assert "--env-file /dev/null -p managed-local-" in cleanup
        assert cleanup.endswith("down -v")
        assert list((tmp_path / "test-results").glob("*/owned.log"))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        unrelated.terminate()
        unrelated.wait()
