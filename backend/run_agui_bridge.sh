#!/bin/sh
exec python -m uvicorn managed_agents_app.agui_bridge.app:production_app --factory --host 0.0.0.0 --port "${PORT:-8080}"
