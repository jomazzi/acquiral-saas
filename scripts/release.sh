#!/usr/bin/env sh
# Run once per deploy, BEFORE the new app instance(s) start serving
# traffic -- as a Render "pre-deploy command", a Railway/Fly release
# command, or by hand after `docker run` on a plain VPS. Idempotent:
# safe to run on every deploy, including ones with no new migration.
#
# Deliberately a separate step from the app's own startup (see the
# Dockerfile) so that:
#  - a failed migration stops the deploy instead of leaving a
#    half-migrated database behind a newly-started app;
#  - scaling to multiple app instances never runs this more than once
#    per deploy (a container's own CMD runs once per INSTANCE; a
#    release command runs once per DEPLOY).
set -e

echo "Running database migrations..."
flask db upgrade

echo "Applying Row-Level Security policies (idempotent)..."
psql "$DATABASE_URL" -f scripts/setup_rls.sql

echo "Release step complete."
