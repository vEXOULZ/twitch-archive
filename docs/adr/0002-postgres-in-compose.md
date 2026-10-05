# ADR-0002: Postgres runs in compose

**Status:** Accepted — 2026-10-04
**Date:** 2026-10-04
**Deciders:** Project owner

## Context

`compose.yaml` ran the api and the worker with host networking, next to a PostgreSQL installed natively
on the same host (`127.0.0.1:5432`). So the stack wasn't self-contained: a new host needed a database
server installed and configured by hand, the schema and role steps needed root on that host
(`deploy/apply-roles.sh` ran `psql` as the `postgres` user), and an operator had to remember to run them
after a release that changed `migrations/`. Host networking also gave both containers every port on the
host. doomtp-bot and vexoulz-auth, the other two Python services, already run Postgres 17 as a compose
service.

## Decision

- **A `db` service:** `postgres:17-alpine`, on the named volume `pgdata`, its superuser password from the
  docker secret `postgres_password`. Nothing is published; only the compose network reaches it.
- **The api and the worker leave host networking.** Their ports are published on `ARCHIVE_PUBLISH_ADDR`
  (default `127.0.0.1`), and their database URLs point at `db`.
- **Two one-shot steps run on every `up`, before the services:** `migrate` (`alembic upgrade head` as the
  superuser, from the api image, as doomtp-bot's ADR-0022 does) and then `roles` (`deploy/roles.sql`, from
  the Postgres image). The api and worker depend on both having succeeded, so no deploy skips them and a
  failed one leaves the running containers alone. `deploy/apply-roles.sh` and `secrets/admin.env` go.
- **Each password lives in one file.** The superuser's is `secrets/postgres_password`; the `migrate` step
  builds its URL from it. The role passwords are read out of `secrets/api_database_url` and
  `secrets/worker_database_url`. So all three must be URL-safe (`openssl rand -hex 32`).
- **A `backup` service** under the `tools` profile writes `pg_dump -Fc` archives and keeps seven.

## Consequences

- `docker compose up -d` on a fresh host brings up a working stack with its own database.
- Moving an existing database is a dump and a `pg_restore` (README §7.4), done once per host.
- The migrations' rule is unchanged: they stay additive, because the old containers run against the
  migrated schema until the new ones start.
- Postgres upgrades are an image tag change plus a dump and restore across major versions, as for the
  other two services.
