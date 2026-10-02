#!/bin/sh
exec python -m uvicorn managed_agents_app.a2a_event_sink.app:production_app --factory --host 0.0.0.0 --port "${PORT:-8080}"
