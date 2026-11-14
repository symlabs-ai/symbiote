#!/usr/bin/env bash
# Run the test suite against PostgreSQL.
#
# Starts a throwaway postgres:15-alpine container (data in memory, bound to
# 127.0.0.1 on a random port), runs pytest with SYMBIOTE_TEST_POSTGRES_DSN
# set, and removes the container. Extra arguments go to pytest:
#
#   scripts/test-postgres.sh                 # whole suite
#   scripts/test-postgres.sh tests/unit -x   # a subset
#
# To use a server you already have, skip this script and export
# SYMBIOTE_TEST_POSTGRES_DSN yourself (every test creates and drops its own
# schema; the role needs CREATE on the database).
set -euo pipefail

IMAGE="${SYMBIOTE_TEST_POSTGRES_IMAGE:-postgres:15-alpine}"
NAME="symbiote-test-pg-$$"
PYTHON="${PYTHON:-python}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --rm --name "$NAME" \
    -e POSTGRES_HOST_AUTH_METHOD=trust \
    -p 127.0.0.1::5432 \
    --tmpfs /var/lib/postgresql/data \
    "$IMAGE" \
    -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=300 \
    >/dev/null

PORT="$(docker port "$NAME" 5432/tcp | head -n1 | sed 's/.*://')"
for _ in $(seq 1 60); do
    if docker exec "$NAME" psql -U postgres -tAc "SELECT 1" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

export SYMBIOTE_TEST_POSTGRES_DSN="postgresql://postgres@127.0.0.1:${PORT}/postgres"
if [ "$#" -eq 0 ]; then
    set -- tests demo/tests
fi
"$PYTHON" -m pytest "$@"
