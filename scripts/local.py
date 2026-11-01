#!/usr/bin/env python3
"""Foreground fake-provider stack. No dotenv, dependency installation, or detached processes."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "backend/.venv/bin/python"


def local_env(ports: dict[str, int]) -> dict[str, str]:
    names = (
        "PATH",
        "HOME",
        "USER",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_CONFIG",
        "PLAYWRIGHT_BROWSERS_PATH",
    )
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_CACHE_DIR": str(ROOT / ".uv-cache"),
            "APP_ENV": "e2e",
            "DATABASE_MODE": "postgres",
            "AWS_EC2_METADATA_DISABLED": "true",
            "DATABASE_URL": f"postgresql://managed_agents:managed_agents@127.0.0.1:{ports['postgres']}/managed_agents_e2e",
            "CLAUDE_AGENT_ID": "agent_e2e",
            "CLAUDE_AGENT_VERSION": "1",
            "CLAUDE_ENVIRONMENT_ID": "env_e2e",
            "WEB_ACCESS_TOKEN": "e2e-access-token",
            "WEB_COOKIE_SECRET": "e2e-cookie-secret-at-least-32-bytes",
            "DEV_PRINCIPAL_ID": "00000000-0000-4000-8000-000000000001",
            "PUBLIC_APP_URL": f"http://127.0.0.1:{ports['frontend']}",
            "OPENAPPA_BINARY_PATH": str(ROOT / ".generated/openappa-bin/openappa-cma"),
            "NEXT_PUBLIC_API_BASE_URL": f"http://127.0.0.1:{ports['backend']}",
            "E2E_BASE_URL": f"http://127.0.0.1:{ports['frontend']}",
            "E2E_BACKEND_URL": f"http://127.0.0.1:{ports['backend']}",
            "E2E_POSTGRES_PORT": str(ports["postgres"]),
            "LOCAL_BACKEND_URL": f"http://127.0.0.1:{ports.get('controller', ports['backend'] + 1)}",
            "LOCAL_A2A_URL": f"http://127.0.0.1:{ports.get('controller', ports['backend'] + 1)}/a2a",
            "CMA_A2A_ENDPOINT": f"http://127.0.0.1:{ports.get('controller', ports['backend'] + 1)}/a2a",
            "A2A_EVENT_SINK_URL": "http://a2a-sink.local/internal/a2a/events",
            "AWS_LAMBDA_HTTP_ENDPOINT": f"127.0.0.1:{ports['backend']}",
            "PREVIEW_ENABLED": "true",
            "SLACK_PREVIEW_ENABLED": "true",
            "APPSYNC_HTTP_URL": f"http://127.0.0.1:{ports.get('preview', 3003)}/event",
            "APPSYNC_WEBSOCKET_URL": f"ws://127.0.0.1:{ports.get('preview', 3003)}/event/realtime",
            "LOCAL_PREVIEW_PORT": str(ports.get("preview", 3003)),
            "LOCAL_CMA_URL": f"http://127.0.0.1:{ports.get('controller', ports['backend'] + 1)}",
            "LOCAL_EVENT_BUS_AUTO_DRAIN": "true",
        }
    )
    return env


def prerequisites() -> None:
    missing = [name for name in ("node", "npm", "docker") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Install missing prerequisites: " + ", ".join(missing))
    if not PYTHON.exists() or not (ROOT / "node_modules/next/package.json").exists():
        raise RuntimeError("Install dependencies with npm ci and uv sync --directory backend --locked.")
    if any(p.name != ".env.example" for p in (ROOT / "apps/web").glob(".env*")):
        raise RuntimeError("Remove apps/web/.env* files before local runs; Next would load live settings.")


def ports() -> dict[str, int]:
    # Reserve all choices together. A later competing bind fails startup;
    # the runner never kills a listener or tears down another Compose project.
    with contextlib.ExitStack() as stack:
        result = {}
        for name, preferred in (
            ("frontend", 3000),
            ("backend", 3001),
            ("controller", 3002),
            ("preview", 3003),
            ("postgres", 54321),
        ):
            listener = stack.enter_context(socket.socket())
            try:
                listener.bind(("127.0.0.1", preferred))
            except OSError:
                listener.bind(("127.0.0.1", 0))
            result[name] = listener.getsockname()[1]
        return result


def request(url: str, data: dict | None = None) -> None:
    payload = json.dumps(data).encode() if data is not None else None
    with urllib.request.urlopen(
        urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}), timeout=2
    ) as response:
        if response.status != 200:
            raise RuntimeError("Local readiness failed")


class Stack:
    def __init__(self, mode: str):
        self.mode = mode
        self.ports = ports()
        self.env = local_env(self.ports)
        self.env["LOCAL_EVENT_BUS_AUTO_DRAIN"] = str(mode == "dev").lower()
        self.project = "managed-local-" + uuid4().hex[:12]
        self.artifacts = ROOT / "test-results" / self.project
        self.artifacts.mkdir(parents=True)
        self.children: list[subprocess.Popen] = []
        self.compose = [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-p",
            self.project,
            "-f",
            str(ROOT / "docker-compose.e2e.yml"),
        ]
        self.compose_started = False

    def run(self, command: list[str], *, timeout: int = 300, env: dict[str, str] | None = None) -> None:
        child = subprocess.Popen(command, cwd=ROOT, env=env or self.env, start_new_session=True)
        self.children.append(child)
        try:
            code = child.wait(timeout=timeout)
            if code:
                raise subprocess.CalledProcessError(code, command)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
        finally:
            # On interruption, close() owns the whole command process group,
            # including browser descendants, just like the long-running services.
            if child.poll() is not None:
                self.children.remove(child)

    def spawn(self, command: list[str], name: str) -> None:
        with (self.artifacts / (name + ".log")).open("w") as output:
            self.children.append(
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=self.env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            )

    def start(self) -> None:
        self.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        self.run(["bash", "scripts/build_openappa.sh"])
        self.compose_started = True
        self.run(self.compose + ["up", "-d", "--wait", "--wait-timeout", "60"])
        self.run([str(PYTHON), "-m", "managed_agents_app.operations", "migrate"])
        if self.mode == "e2e":
            self.run(
                [
                    str(PYTHON),
                    "-m",
                    "pytest",
                    "backend/tests/integration",
                    "--ignore=backend/tests/integration/test_real_services.py",
                ],
                env=self.env | {"RUN_POSTGRES_TESTS": "1"},
                timeout=600,
            )
            self.run(
                [
                    "node",
                    "--import",
                    "tsx",
                    "--test",
                    "apps/web-function/tests/repository.postgres.ts",
                    "apps/slack-preview/tests/repository.postgres.ts",
                ]
            )
        backend = [
            str(PYTHON),
            "-m",
            "uvicorn",
            "managed_agents_app.local_api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.ports["controller"]),
        ]
        if self.mode == "dev":
            backend += ["--reload", "--reload-dir", str(ROOT / "backend/src")]
        self.spawn(backend, "backend")
        self.spawn(["node", "--import", "tsx", "apps/cma-preview/src/local.ts"], "preview")
        if self.mode == "production":
            self.run(["npm", "run", "build:functions"])
            self.spawn(["node", "dist/web-function/index.js"], "web-function")
        else:
            self.spawn(["node", "--import", "tsx", "apps/web-function/src/index.ts"], "web-function")
        if self.mode == "production":
            self.run(["npm", "run", "build:web"])
            self.spawn(
                [
                    str(PYTHON),
                    "-m",
                    "http.server",
                    str(self.ports["frontend"]),
                    "--bind",
                    "127.0.0.1",
                    "--directory",
                    "apps/web/out",
                ],
                "frontend",
            )
        else:
            self.spawn(
                [
                    "node",
                    "node_modules/next/dist/bin/next",
                    "dev",
                    "apps/web",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(self.ports["frontend"]),
                ],
                "frontend",
            )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in self.children):
                raise RuntimeError(f"A service exited; inspect {self.artifacts}")
            try:
                request(self.env["E2E_BACKEND_URL"] + "/_test/instance")
                request(self.env["E2E_BASE_URL"])
                request(self.env["E2E_BACKEND_URL"] + "/_test/reset", {})
                return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError(f"Readiness timed out; inspect {self.artifacts}")

    def close(self) -> None:
        for child in reversed(self.children):
            if child.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
        for child in reversed(self.children):
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        if self.compose_started:
            self.run(self.compose + ["down", "-v"], timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dev", "e2e", "production"))
    args, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra = extra[1:]
    stack = None

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        prerequisites()
        stack = Stack(args.mode)
        print(f"Logs: {stack.artifacts}", flush=True)
        stack.start()
        print(f"Web: {stack.env['E2E_BASE_URL']}  API: {stack.env['E2E_BACKEND_URL']}", flush=True)
        if args.mode == "dev":
            while all(child.poll() is None for child in stack.children):
                time.sleep(0.5)
            raise RuntimeError(f"A service exited; inspect {stack.artifacts}")
        config = "playwright.production.config.ts" if args.mode == "production" else "playwright.config.ts"
        stack.run(
            ["node", "node_modules/@playwright/test/cli.js", "test", "--config=" + config, *extra],
            timeout=1800,
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Local run failed: {error}", file=sys.stderr)
        return 1
    finally:
        # npm/terminal can forward a second interrupt. Once cleanup starts, finish
        # reaping the owned groups and Compose project before honoring exit 130.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if stack:
            stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
