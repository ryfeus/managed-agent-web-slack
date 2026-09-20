#!/bin/sh
exec python -m uvicorn managed_agents_app.handlers.web_stream:app --host 0.0.0.0 --port "${PORT:-8080}"
