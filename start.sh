#!/bin/sh
# Container entrypoint: apply migrations, then serve.
set -e
alembic upgrade head
# --proxy-headers: the platform's load balancer sets X-Forwarded-For, which rate limiting relies on.
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*" --ws-ping-interval 20
