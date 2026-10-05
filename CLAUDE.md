@.conventions/CLAUDE.md

# twitch-archive

Records and archives Twitch VODs: `services/worker` captures, processes and uploads them; `services/api`
serves the read-only, Feathers-compatible API the sites use. `packages/common` is shared by both. The
README is the full manual.

- Pull requests go to `dev`; `main` is production and changes only through a release
  (docs/adr/0001). The server rebuilds and restarts on every change to `main`.
- Releases run from `.github/workflows/release.yml` (Actions → release → Run workflow; CONTRIBUTING
  "Release flow"): don't bump, tag or back-merge by hand.
- Migrations are Alembic, in `migrations/`, and must stay additive: the old containers keep running
  against the migrated schema until the new ones start. A schema change is a new revision.
- The API's response shapes are a contract with the sites (golden tests in `tests/api_contract/`).
  Don't change a shape without changing the sites too.
- The database and contract tests skip without a restored copy of the database (README §9), and so
  skip in CI. Run them locally before merging anything that touches queries.
- Long job steps save their progress in `ctx.payload` and must be safe to re-run part-way.
- mypy is strict. Existing code carries inline `# type: ignore[...]` from when it was turned on; don't
  add new ones without a reason in the same comment.
- vex-platform is pinned by tarball in `packages/common/pyproject.toml` and moves only to a tagged
  release.
