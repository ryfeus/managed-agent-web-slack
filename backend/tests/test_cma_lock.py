from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def lock_module():
    spec = importlib.util.spec_from_file_location("cma_lock", ROOT / "scripts/cma_lock.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_data(version="7"):
    return {
        "version": 1,
        "origin": {
            "base_url": "https://api.anthropic.com",
            "organization_id": "00000000-0000-4000-8000-000000000001",
            "workspace_id": "wrkspc_test",
        },
        "resources": {
            "./agents/application.md": {
                "kind": "agent",
                "id": "agent_test",
                "version": version,
                "hash": "local",
                "remote_hash": "remote",
            },
            "./environments/application.yaml": {
                "kind": "environment",
                "id": "env_test",
                "hash": "local",
                "remote_hash": "remote",
            },
        },
    }


@pytest.fixture
def project(tmp_path):
    for name in ("agents/application.md", "environments/application.yaml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("name: test\n")
    (tmp_path / "claude-lock.json").write_text(json.dumps(lock_data()))
    return tmp_path


@pytest.mark.parametrize("version", [7, "7"])
def test_lock_resolves_exact_version(project, version):
    assert lock_module().parse_lock(lock_data(version), project) == {
        "agent_id": "agent_test",
        "agent_version": 7,
        "environment_id": "env_test",
    }


@pytest.mark.parametrize("version", [None, True, False, 0, -1, 1.5, "1.5", "", "latest", "-1"])
def test_invalid_versions(project, version):
    module = lock_module()
    with pytest.raises(module.LockError, match="positive integer"):
        module.parse_lock(lock_data(version), project)


@pytest.mark.parametrize(
    "kind,path", [("agent", "agents/application.md"), ("environment", "environments/application.yaml")]
)
@pytest.mark.parametrize("failure", ["missing", "kind", "id", "hash", "remote_hash"])
def test_invalid_resource(project, kind, path, failure):
    data = lock_data()
    resource = data["resources"][f"./{path}"]
    if failure == "missing":
        del data["resources"][f"./{path}"]
    elif failure == "kind":
        resource["kind"] = "deployment"
    else:
        resource[failure] = None
    module = lock_module()
    with pytest.raises(module.LockError):
        module.parse_lock(data, project)


@pytest.mark.parametrize("origin", [None, {}, {"base_url": "https://api.anthropic.com"}])
def test_missing_origin(project, origin):
    data = lock_data()
    data["origin"] = origin
    module = lock_module()
    with pytest.raises(module.LockError, match="origin"):
        module.parse_lock(data, project)


@pytest.mark.parametrize(
    "url",
    ["http://api.anthropic.com", "https://secret@api.anthropic.com", "https://api.anthropic.com?key=secret"],
)
def test_unsafe_origin(project, url):
    data = lock_data()
    data["origin"]["base_url"] = url
    module = lock_module()
    with pytest.raises(module.LockError, match="HTTPS"):
        module.parse_lock(data, project)


@pytest.mark.parametrize("field", ["organization_id", "workspace_id"])
def test_malformed_origin_identity(project, field):
    data = lock_data()
    data["origin"][field] = "malformed"
    module = lock_module()
    with pytest.raises(module.LockError, match="origin"):
        module.parse_lock(data, project)


def test_ambiguous_paths(project):
    data = lock_data()
    data["resources"]["agents/application.md"] = data["resources"]["./agents/application.md"]
    module = lock_module()
    with pytest.raises(module.LockError, match="ambiguous"):
        module.parse_lock(data, project)


@pytest.mark.parametrize("path", ["../agents/application.md", "/agents/application.md", "agents/other.md"])
def test_unexpected_paths(project, path):
    data = lock_data()
    data["resources"][path] = data["resources"].pop("./agents/application.md")
    module = lock_module()
    with pytest.raises(module.LockError, match="unexpected"):
        module.parse_lock(data, project)


@pytest.mark.parametrize("failure", ["missing", "malformed", "duplicate"])
def test_invalid_lock_file(project, failure):
    path = project / "claude-lock.json"
    if failure == "missing":
        path.unlink()
    else:
        path.write_text("{" if failure == "malformed" else '{"version":1,"version":1}')
    module = lock_module()
    with pytest.raises(module.LockError):
        module.read_lock(project)


def test_missing_agent_version(project):
    data = lock_data()
    del data["resources"]["./agents/application.md"]["version"]
    module = lock_module()
    with pytest.raises(module.LockError, match="positive integer"):
        module.parse_lock(data, project)


@pytest.mark.parametrize("path", ["agents/application.md", "environments/application.yaml"])
def test_missing_source(project, path):
    (project / path).unlink()
    module = lock_module()
    with pytest.raises(module.LockError, match="source"):
        module.read_lock(project)


@pytest.mark.parametrize(
    "content", ['api_key: "secret"', 'authorization: "Bearer secret"', "sk-ant-" + "x" * 25]
)
def test_secret_sources_rejected(project, content):
    (project / "agents/application.md").write_text(content)
    module = lock_module()
    with pytest.raises(module.LockError, match="secret"):
        module.read_lock(project)


def test_source_symlink_cannot_escape_project(project, tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("name: outside")
    source = project / "agents/application.md"
    source.unlink()
    source.symlink_to(outside)
    module = lock_module()
    with pytest.raises(module.LockError, match="unsafe"):
        module.read_lock(project)


def test_nested_and_escaped_lock_secrets_are_rejected(project):
    data = lock_data()
    data["origin"]["authorization"] = "Bearer secret"
    module = lock_module()
    with pytest.raises(module.LockError, match="secret"):
        module.parse_lock(data, project)
    del data["origin"]["authorization"]
    data["note"] = "sk-ant-" + "x" * 25
    (project / "claude-lock.json").write_text(json.dumps(data).replace("sk-ant-", "sk\\u002dant-"))
    with pytest.raises(module.LockError, match="secret"):
        module.read_lock(project)
