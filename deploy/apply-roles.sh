#!/bin/bash
# Create or update the archive_api / archive_worker database roles (deploy/roles.sql).
# Run as root on the database host, from anywhere:
#
#   bash /path/to/twitch-archive/deploy/apply-roles.sh
#
# Passwords come from secrets/admin.env (API_DB_PASSWORD, WORKER_DB_PASSWORD).
# The SQL is fed on stdin because the postgres user usually cannot read the checkout.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
. secrets/admin.env
set +a
runuser -u postgres -- psql -d archive -X -q \
  -v api_password="$API_DB_PASSWORD" -v worker_password="$WORKER_DB_PASSWORD" \
  -f - < deploy/roles.sql
echo "roles applied: archive_api, archive_worker"
