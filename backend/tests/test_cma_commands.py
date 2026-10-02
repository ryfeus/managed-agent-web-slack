from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_cma_lock import lock_data

ROOT = Path(__file__).resolve().parents[2]
STUB = r"""
import json
import os
import sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['COMMAND_LOG'], 'a') as stream:
    stream.write(json.dumps({'name': name, 'args': args, 'cwd': str(Path.cwd()),
        'pins': {k: v for k, v in os.environ.items() if k.startswith('TF_VAR_claude_')},
        'runtime': {k: v for k, v in os.environ.items() if k.startswith('CLAUDE_')}}) + '\n')
if name == 'ant':
    if args == ['--version']:
        print(os.getenv('MOCK_ANT_VERSION', 'ant version 1.36.0'))
    elif args == ['apply', '--yes', '.']:
        if os.getenv('MOCK_ANT_MUTATE') == '1' or not Path('claude-lock.json').exists():
            Path('claude-lock.json').write_text(Path('../next-lock.json').read_text())
        sys.exit(int(os.getenv('MOCK_ANT_FAILURE', '0')))
elif name == 'aws':
    if args[:2] == ['sts', 'get-caller-identity']:
        print('123456789012')
    elif args[:2] == ['configure', 'export-credentials']:
        print('export AWS_ACCESS_KEY_ID=mock AWS_SECRET_ACCESS_KEY=mock AWS_SESSION_TOKEN=mock')
elif name == 'terraform':
    if 'show' in args:
        print('{"resource_changes":[]}')
    elif 'output' in args:
        if 'dsql_runtime_role_arns' in args:
            print('[]')
        elif 'application_url' in args:
            print('https://example.test')
        else:
            print('test-resource')
    elif 'apply' in args:
        sys.exit(int(os.getenv('MOCK_TERRAFORM_FAILURE', '0')))
elif name == 'git':
    if os.getenv('MOCK_GIT_DIRTY') == '1':
        print(' M cma/claude-lock.json')
"""


@pytest.fixture
def sandbox(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "cma.sh",
        "cma_lock.py",
        "cma_env.sh",
        "cma-controller-local.sh",
        "deploy.sh",
        "render_terraform_backend.py",
        "deploy_guard.jq",
    ):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    for name in ("build_python_lambdas.sh", "render_slack_manifest.py"):
        target = scripts / name
        target.write_text("#!/usr/bin/env bash\nexit 0\n" if name.endswith(".sh") else "")
        target.chmod(0o755)
    (tmp_path / "infra/app").mkdir(parents=True)
    (tmp_path / "infra/bootstrap").mkdir(parents=True)
    for name in ("agents/application.md", "environments/application.yaml"):
        target = tmp_path / "cma" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("name: test\n")
    (tmp_path / "cma/claude-lock.json").write_text(json.dumps(lock_data()))
    (tmp_path / "next-lock.json").write_text(json.dumps(lock_data(9)))
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=test-key\nWEB_ACCESS_TOKEN=test-token\nWEB_COOKIE_SECRET=test-cookie\n"
        "CLAUDE_AGENT_ID=agent_stale\nCLAUDE_AGENT_VERSION=100\nCLAUDE_ENVIRONMENT_ID=env_stale\nAGENT_ID=agent_old\n"
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("ant", "aws", "terraform", "npm", "git", "uv"):
        target = binaries / name
        target.write_text(f"#!{sys.executable}\n" + STUB)
        target.chmod(0o755)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "TF_VAR_", "CLAUDE_", "SLACK_", "MOCK_"))
    }
    env.update(PATH=f"{binaries}:{env['PATH']}", COMMAND_LOG=str(tmp_path / "commands.jsonl"))
    return tmp_path, env


def run(sandbox, script, *args, **overrides):
    root, env = sandbox
    return subprocess.run(
        ["bash", str(root / "scripts" / script), *args],
        cwd=root.parent,
        env=env | overrides,
        text=True,
        capture_output=True,
        timeout=30,
    )


def commands(sandbox):
    path = sandbox[0] / "commands.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_show_is_offline(sandbox):
    result = run(sandbox, "cma.sh", "show")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["agent_version"] == 7
    assert not commands(sandbox)


def test_local_controller_launcher_resolves_lock_before_start(sandbox):
    result = run(sandbox, "cma-controller-local.sh")
    assert result.returncode == 0, result.stderr
    started = next(c for c in commands(sandbox) if c["name"] == "uv")
    assert started["runtime"] == {
        "CLAUDE_AGENT_ID": "agent_test",
        "CLAUDE_AGENT_VERSION": "7",
        "CLAUDE_ENVIRONMENT_ID": "env_test",
    }
    assert started["args"][-4:] == ["--host", "127.0.0.1", "--port", "8081"]


def test_packaged_controller_launcher_needs_no_repository_files(tmp_path):
    launcher = tmp_path / "run.sh"
    shutil.copy2(ROOT / "backend/run_cma_controller.sh", launcher)
    python = tmp_path / "python"
    python.write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    python.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PORT"}
    result = subprocess.run(
        ["sh", str(launcher)],
        cwd=tmp_path,
        env=env | {"PATH": f"{tmp_path}:{env['PATH']}"},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "-m",
        "uvicorn",
        "managed_agents_app.cma_controller.app:production_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
    ]


@pytest.mark.parametrize("command,flag", [("plan", "--dry-run"), ("apply", "--yes")])
def test_explicit_apply_directory_and_exact_flags(sandbox, command, flag):
    before = (sandbox[0] / "cma/claude-lock.json").read_bytes()
    result = run(sandbox, "cma.sh", command)
    assert result.returncode == 0, result.stderr
    apply = next(c for c in commands(sandbox) if c["args"][0] == "apply")
    assert apply["args"] == ["apply", flag, "."]
    assert apply["cwd"] == str(sandbox[0] / "cma")
    assert (sandbox[0] / "cma/claude-lock.json").read_bytes() == before


@pytest.mark.parametrize("version", ["ant version 1.29.9", "unknown"])
def test_unsupported_cli_fails_before_apply(sandbox, version):
    result = run(sandbox, "cma.sh", "apply", MOCK_ANT_VERSION=version)
    assert result.returncode != 0
    assert "1.30.0" in result.stderr
    assert all(c["args"] == ["--version"] for c in commands(sandbox))


def test_explicit_bootstrap_creates_real_cli_lock(sandbox):
    (sandbox[0] / "cma/claude-lock.json").unlink()
    result = run(sandbox, "cma.sh", "apply")
    assert result.returncode == 0, result.stderr
    assert (
        json.loads((sandbox[0] / "cma/claude-lock.json").read_text())["resources"]["./agents/application.md"][
            "version"
        ]
        == 9
    )


def test_drift_failure_preserves_exit_and_never_forces(sandbox):
    result = run(sandbox, "cma.sh", "apply", MOCK_ANT_FAILURE="13")
    assert result.returncode == 13
    assert "Inspect the provider diff" in result.stderr
    assert commands(sandbox)[-1]["args"] == ["apply", "--yes", "."]


def test_explicit_apply_can_resume_partial_bootstrap(sandbox):
    data = lock_data()
    del data["resources"]["./environments/application.yaml"]
    (sandbox[0] / "cma/claude-lock.json").write_text(json.dumps(data))
    assert run(sandbox, "deploy.sh").returncode != 0
    result = run(sandbox, "cma.sh", "apply", MOCK_ANT_MUTATE="1")
    assert result.returncode == 0, result.stderr


def test_apply_rejects_invalid_lock_before_cli(sandbox):
    (sandbox[0] / "cma/claude-lock.json").write_text("{")
    assert run(sandbox, "cma.sh", "apply").returncode != 0
    assert commands(sandbox) == []


def test_deploy_plan_only_uses_lock_without_ant(sandbox):
    result = run(sandbox, "deploy.sh", "--plan-only")
    assert result.returncode == 0, result.stderr
    log = commands(sandbox)
    assert not any(c["name"] == "ant" for c in log)
    assert not any(c["name"] == "terraform" and "apply" in c["args"] for c in log)
    plan = next(c for c in log if c["name"] == "terraform" and "plan" in c["args"])
    assert plan["pins"] == {
        "TF_VAR_claude_agent_id": "agent_test",
        "TF_VAR_claude_agent_version": "7",
        "TF_VAR_claude_environment_id": "env_test",
    }


def test_deploy_uses_updated_lock_before_aws_apply(sandbox):
    result = run(sandbox, "deploy.sh", MOCK_ANT_MUTATE="1")
    assert result.returncode == 0, result.stderr
    log = commands(sandbox)
    apply = next(i for i, c in enumerate(log) if c["name"] == "ant" and "apply" in c["args"])
    aws_apply = next(i for i, c in enumerate(log) if c["name"] == "terraform" and "apply" in c["args"])
    assert apply < aws_apply
    assert log[aws_apply]["pins"]["TF_VAR_claude_agent_version"] == "9"
    assert log[aws_apply]["pins"]["TF_VAR_claude_agent_id"] == "agent_test"


@pytest.mark.parametrize("bad", ["missing", "malformed"])
def test_bad_lock_stops_deployment_before_services(sandbox, bad):
    path = sandbox[0] / "cma/claude-lock.json"
    if bad == "missing":
        path.unlink()
    else:
        path.write_text("{")
    result = run(sandbox, "deploy.sh")
    assert result.returncode != 0
    assert not any(c["name"] in {"ant", "aws", "terraform"} for c in commands(sandbox))


@pytest.mark.parametrize("failure", ["anthropic", "terraform"])
def test_partial_failure_keeps_updated_lock_and_warns(sandbox, failure):
    options = {"MOCK_ANT_MUTATE": "1", "MOCK_GIT_DIRTY": "1"}
    options["MOCK_ANT_FAILURE" if failure == "anthropic" else "MOCK_TERRAFORM_FAILURE"] = "13"
    result = run(sandbox, "deploy.sh", **options)
    assert result.returncode == 13
    assert "Review and commit" in result.stderr
    assert (
        json.loads((sandbox[0] / "cma/claude-lock.json").read_text())["resources"]["./agents/application.md"][
            "version"
        ]
        == 9
    )
