#!/bin/sh
set -eu
exec python -m uvicorn managed_agents_app.cma_controller.app:production_app --factory --host 0.0.0.0 --port "${PORT:-8080}"
