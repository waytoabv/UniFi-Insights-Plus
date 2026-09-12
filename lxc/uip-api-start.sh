#!/bin/bash
# Starts uvicorn for the API/dashboard unit.
#
# Exists only to reproduce the lowercase log-level derivation that entrypoint.sh
# performs before exec'ing supervisord — systemd cannot transform an environment
# variable, and supervisord.conf interpolates %(ENV_UVICORN_LOG_LEVEL)s.
set -e

UVICORN_LOG_LEVEL=$(echo "${LOG_LEVEL:-INFO}" | tr '[:upper:]' '[:lower:]')

case "$UVICORN_LOG_LEVEL" in
    critical|error|warning|info|debug|trace) ;;
    *) UVICORN_LOG_LEVEL=info ;;
esac

exec /app/venv/bin/uvicorn api:app \
    --host 0.0.0.0 \
    --port "${API_PORT:-8000}" \
    --workers 1 \
    --log-level "$UVICORN_LOG_LEVEL"
