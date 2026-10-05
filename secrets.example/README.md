# secrets.example

Copy this directory to `secrets/` (gitignored) and fill in the values. Each file holds one value and
nothing else. `compose.yaml` mounts them as docker secrets at `/run/secrets/<name>`, and the settings
are read from there. A variable in the environment still wins over its file.

On a server: mode 0400, owned by uid 1000, the user both images run as. A file that is empty leaves
its setting at the default (off, for the optional ones), but every file must exist or compose refuses
to start.

Every password here is generated (`openssl rand -hex 32`): the `migrate` and `roles` steps put them into
URLs and SQL as they are, so they must be URL-safe.

| File | Mounted in | As | Notes |
|---|---|---|---|
| `postgres_password` | db, migrate, roles, backup | | The `postgres` superuser. `db` sets it when it creates an empty volume; changing it later needs `ALTER ROLE` too |
| `api_database_url` | api, roles | `archive_database_url` | `archive_api` role; `roles` sets the role's password from it |
| `worker_database_url` | worker, roles | `archive_database_url` | `archive_worker` role; `roles` sets the role's password from it |
| `archive_twitch_client_secret` | api, worker | | Twitch app; `/v2/badges` in the api |
| `archive_admin_api_key` | worker | | Admin API bearer key (`openssl rand -hex 32`) |
| `archive_admin_password` | worker | | Dashboard password; empty turns password login off |
| `archive_admin_auth_client_secret` | worker | | Twitch sign-in through vexoulz-auth; empty turns it off |
| `archive_google_client_secret` | worker | | YouTube uploads |
| `archive_doomtp_api_key` | worker | | doomtp-bot read-scope key; empty uses the public log |
