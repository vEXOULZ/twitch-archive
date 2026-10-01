-- Least-privilege database roles for the two services. Idempotent: re-run it
-- after every migration that adds tables (it also resets the passwords).
--
--   bash deploy/apply-roles.sh        (as root; reads secrets/admin.env)
--
-- Migrations themselves run as the postgres superuser
-- (ARCHIVE_MIGRATION_DATABASE_URL), never as these roles.

\set ON_ERROR_STOP on

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archive_api') AS create_api \gset
\if :create_api
CREATE ROLE archive_api LOGIN;
\endif
ALTER ROLE archive_api LOGIN PASSWORD :'api_password';

SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archive_worker') AS create_worker \gset
\if :create_worker
CREATE ROLE archive_worker LOGIN;
\endif
ALTER ROLE archive_worker LOGIN PASSWORD :'worker_password';

GRANT CONNECT ON DATABASE archive TO archive_api, archive_worker;
GRANT USAGE ON SCHEMA public TO archive_api, archive_worker;

-- archive-api: read only, and only the tables the public API serves.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM archive_api;
GRANT SELECT ON vods, games, emotes, logs, bot_logs, streams, vod_segments TO archive_api;

-- archive-worker: DML on the app tables (no DDL).
GRANT SELECT, INSERT, UPDATE, DELETE ON vods, games, emotes, logs, bot_logs, streams, jobs, app_state, job_events,
    vod_splices, vod_splice_logs, vod_splice_bot_logs, vod_segments TO archive_worker;
-- The audit log is append-only for the worker.
GRANT SELECT, INSERT ON admin_audit TO archive_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON admin_sessions, settings TO archive_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO archive_worker;

-- vex-platform (Alembic 0012). audit_log is append-only too. The jobs schema is procrastinate's queue
-- plus job_runs/job_run_events: the worker needs DML on all of it, because procrastinate's triggers
-- run as the caller and write its other tables (events, periodic defers).
GRANT SELECT, INSERT ON audit_log TO archive_worker;
GRANT USAGE ON SCHEMA jobs TO archive_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA jobs TO archive_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA jobs TO archive_worker;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA jobs TO archive_worker;
