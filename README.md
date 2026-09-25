# twitch-archive

A Python rewrite of the Feathers/Node archive backend (fork of
[TimIsOverpowered/archive](https://github.com/TimIsOverpowered/archive)). It is
split into two services that share one database and one codebase:

| Service | What it does | Port |
|---|---|---|
| **archive-api** | Read-only HTTP API for the React frontend (`Archive-React-Vex`). It responds in the same format as the old Feathers API. | 3030 |
| **archive-worker** | Watches Twitch. For every stream it downloads the VOD (and optionally records the live stream), saves chapters, chat and emotes, splits the video, and uploads it to YouTube. It also exposes an admin HTTP API. | 3031 (private network only) |

Scope: Twitch only. Kick, Google Drive and per-game uploads are gone.

```mermaid
flowchart LR
    subgraph Twitch
      helix[Helix API]
      gql[GQL + usher HLS]
    end
    subgraph worker[archive-worker]
      mon[monitor<br/>every 30s] --> q[(jobs table)]
      q --> run[job runner<br/>capture → finalize → chapters → chat<br/>→ emotes → split → upload → describe → cleanup]
      admin[admin API :3031] --> q
    end
    helix --> mon
    gql --> run
    run -->|ffmpeg| disk[/data/]
    run -->|resumable upload| yt[YouTube]
    run --> pg[(PostgreSQL<br/>vods, logs, emotes, …)]
    pg --> api[archive-api :3030]
    api -->|reverse proxy| fe[React frontend]
```

**How it differs from the old Node app:**

- **Twitch download is up to date.** It uses the new `PlaybackAccessToken` hash, the usher `/vod/v2/` playlists, and the chunked → 1080p fallback on 403. fMP4 init segments are handled. All GQL hashes and client IDs are settings, so the next Twitch rotation is a config change.
- **Jobs are durable.** Every job is a list of steps, and progress is stored in Postgres (the `jobs` table). A restart or crash resumes a job at the first unfinished step; finished steps never run again. Failed steps are retried with backoff (3 attempts).
- **No Redis.** Caches and the rate limiter live in process memory.
- **No config file writes.** The YouTube OAuth token is kept in the `app_state` table.
- **Fixed bugs:** `/v2/badges` (it crashed on every request), `$select`, `chapters[name]` (regex injection, and it now combines with other filters), duplicate `streams` inserts, reupload offsets, and splitting VODs that have no chapters.
- **Schema changes are additive only** (new `jobs` and `app_state` tables, plus two indexes on `logs`), so the old app still runs against the same database.

---

## Contents

1. [Quick start (local development)](#1-quick-start-local-development)
2. [Configuration](#2-configuration)
3. [YouTube OAuth setup](#3-youtube-oauth-setup)
4. [Admin API](#4-admin-api)
5. [Live recordings and multiTrack](#5-live-recordings-and-multitrack)
6. [Public API reference](#6-public-api-reference)
7. [Deployment (Docker Compose)](#7-deployment-docker-compose)
8. [Operations and troubleshooting](#8-operations-and-troubleshooting)
9. [Development](#9-development)

---

## 1. Quick start (local development)

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Docker, and ffmpeg/ffprobe on `PATH` (the worker only).

```bash
uv sync                                        # creates .venv with all three packages
docker compose -f compose.dev.yaml up -d       # Postgres 17 on 127.0.0.1:55433 (postgres/dev)

# optional: load a copy of production data
docker compose -f compose.dev.yaml exec -T db pg_restore -U postgres -d archive --no-owner < archive.dump

uv run alembic upgrade head                    # legacy baseline (no-op on existing tables) + jobs/indexes
uv run archive-api                             # http://127.0.0.1:3030/vods
ARCHIVE_ADMIN_API_KEY=dev uv run archive-worker run --dry-run
```

`--dry-run` downloads and processes everything but never uploads to YouTube and never deletes files. Without Twitch credentials the monitor is disabled, but admin-triggered jobs still work: the GQL and usher endpoints need no credentials.

To point the frontend at your local API, run `Archive-React-Vex` with `REACT_APP_VODS_API_BASE=http://127.0.0.1:3030`.

---

## 2. Configuration

All settings are environment variables with the `ARCHIVE_` prefix. They can also be put in a `.env` file in the working directory. Lists are JSON, e.g. `ARCHIVE_RESTRICTED_GAMES='["Artifact"]'`. The source of truth is `packages/common/archive_common/config.py`.

In production, non-secret settings go in `.env` (see `.env.example`). Secrets go in `secrets/api.env` and `secrets/worker.env` (see `deploy/secrets.example/`), so each container only receives the secrets it needs.

### Shared

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_DATABASE_URL` | `postgresql+asyncpg://postgres:dev@127.0.0.1:55433/archive` | Use the `archive_api` or `archive_worker` role in production |
| `ARCHIVE_MIGRATION_DATABASE_URL` | – | Superuser URL used only by `alembic` (falls back to `ARCHIVE_DATABASE_URL`) |
| `ARCHIVE_CHANNEL` | – | Channel name used in YouTube titles |
| `ARCHIVE_DOMAIN_NAME` | – | Frontend host, used in the "Chat Replay" link in descriptions |
| `ARCHIVE_TIMEZONE` | `UTC` | The date in YouTube titles |
| `ARCHIVE_LOG_LEVEL` | `INFO` | |
| `ARCHIVE_TWITCH_ID` / `ARCHIVE_TWITCH_USERNAME` | – | The channel to archive (numeric user ID and login) |
| `ARCHIVE_TWITCH_CLIENT_ID` / `ARCHIVE_TWITCH_CLIENT_SECRET` | – | Twitch app, used for Helix (app token). The API needs it for `/v2/badges`; the worker needs it for the monitor |

### Twitch GQL (update these when Twitch rotates them)

| Variable | Default |
|---|---|
| `ARCHIVE_GQL_CLIENT_ID` | `kimne78kx3ncx6brgo4mv6wki5h1ko` (twitch.tv web client) |
| `ARCHIVE_GQL_BACKUP_CLIENT_ID` | `kd1unb4b3q4t58fwlpcbzcbnm76a8fp` (used for comment cursors and moments) |
| `ARCHIVE_GQL_HASH_PLAYBACK_TOKEN` | `ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9` |
| `ARCHIVE_GQL_HASH_COMMENTS` | `b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a` |
| `ARCHIVE_GQL_HASH_MOMENTS` | `7399051b2d46f528d5f0eedf8b0db8d485bb1bb4c0a2c6707be6f1290cdcb31a` |
| `ARCHIVE_GQL_HASH_NIELSEN` | `2dbf505ee929438369e68e72319d1106bb3c142e295332fac157c90638968586` |

See [Troubleshooting](#8-operations-and-troubleshooting) for how to find new values.

### archive-api

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_API_HOST` / `ARCHIVE_API_PORT` | `0.0.0.0` / `3030` | |
| `ARCHIVE_PAGINATE_DEFAULT` / `ARCHIVE_PAGINATE_MAX` | `10` / `50` | Same as Feathers |
| `ARCHIVE_RATE_LIMIT_POINTS` / `ARCHIVE_RATE_LIMIT_WINDOW_SECONDS` | `20` / `5` | Per client IP (`cf-connecting-ip` → `x-real-ip` → peer) |
| `ARCHIVE_CACHE_TTL_SECONDS` | `300` | Response cache for `/vods`, `/emotes`, … |

### archive-worker

| Variable | Default | Notes |
|---|---|---|
| `ARCHIVE_DATA_DIR` | `/data` | VODs go in `vods/<vodId>/`, live recordings in `live/<streamId>/` |
| `ARCHIVE_VOD_DOWNLOAD` | `true` | Archive every stream's Twitch VOD |
| `ARCHIVE_CHAT_DOWNLOAD` | `true` | Save the chat replay into `logs` |
| `ARCHIVE_LIVE_RECORD` | `false` | Record the live stream itself; see [§5](#5-live-recordings-and-multitrack) |
| `ARCHIVE_MULTI_TRACK` | `false` | Upload both the VOD copy and the live copy |
| `ARCHIVE_YOUTUBE_UPLOAD` | `true` | |
| `ARCHIVE_YOUTUBE_PUBLIC` | `false` | Public instead of unlisted (for the main copy; see §5) |
| `ARCHIVE_YOUTUBE_DESCRIPTION` | `VOD` | Last line of every description |
| `ARCHIVE_YOUTUBE_KEEPALIVE_HOURS` | `24` | How often the worker refreshes the YouTube token; see [§3](#3-youtube-oauth-setup) |
| `ARCHIVE_RESTRICTED_GAMES` | `[]` | Chapters of these games are left out of uploads |
| `ARCHIVE_MANUAL_STEPS` | `{}` | Steps a job pauses before until you resume it, per job kind, e.g. `{"archive":["upload"]}`; see [Manual steps](#manual-steps) |
| `ARCHIVE_SPLIT_DURATION` | `10800` | Maximum YouTube part length in seconds |
| `ARCHIVE_KEEP_HLS` / `ARCHIVE_KEEP_MP4` | `false` | Keep files after upload |
| `ARCHIVE_DRY_RUN` | `false` | Same as `run --dry-run` |
| `ARCHIVE_MONITOR_INTERVAL_SECONDS` | `30` | How often Helix is checked for a live stream |
| `ARCHIVE_HLS_POLL_INTERVAL_SECONDS` | `60` | VOD playlist polling while the stream is live |
| `ARCHIVE_HLS_NO_CHANGE_THRESHOLD` | `10` | Unchanged polls before the capture is considered complete |
| `ARCHIVE_LIVE_POLL_INTERVAL_SECONDS` | `2` | Live playlist polling |
| `ARCHIVE_LIVE_END_THRESHOLD` | `90` | Polls with no new segments before the recorder checks whether the stream ended |
| `ARCHIVE_SEGMENT_CONCURRENCY` | `4` | Parallel segment downloads |
| `ARCHIVE_ADMIN_HOST` / `ARCHIVE_ADMIN_PORT` | `0.0.0.0` / `3031` | |
| `ARCHIVE_ADMIN_API_KEY` | – | **Required** for any admin call |
| `ARCHIVE_GOOGLE_CLIENT_ID` / `ARCHIVE_GOOGLE_CLIENT_SECRET` | – | Google OAuth client for YouTube |
| `ARCHIVE_GOOGLE_REDIRECT_URL` | `http://localhost:3031/admin/refreshtoken` | Must match a redirect URI on the Google client |

---

## 3. YouTube OAuth setup

The worker needs a refresh token for the YouTube channel. There are two ways to get one.

### Option A: import the token the old app already has

```bash
# from the stack directory; /path/to/archive is the old app's checkout
docker compose run --rm -v /path/to/archive/config:/legacy:ro worker \
  archive-worker import-youtube-token /legacy/config.json
```

This copies `youtube.auth.refresh_token` into the `app_state` table. `ARCHIVE_GOOGLE_CLIENT_ID` and `ARCHIVE_GOOGLE_CLIENT_SECRET` must be the same Google client the old app used; they are `google.client_id` and `google.client_secret` in the old config.

### Option B: run the consent flow again

Google only accepts `http://` redirect URIs for `localhost`, so open an SSH tunnel to the worker's admin port and do the flow in your own browser:

1. In Google Cloud Console → Credentials → your OAuth client, make sure an authorized redirect URI matches `ARCHIVE_GOOGLE_REDIRECT_URL`. The old app registered `http://localhost:3030/admin/refreshtoken/`. You can either reuse it (set `ARCHIVE_GOOGLE_REDIRECT_URL` to it and tunnel local port 3030) or add `http://localhost:3031/admin/refreshtoken`.
2. Open the tunnel: `ssh -L 3031:127.0.0.1:3031 user@your-server` (use `-L 3030:127.0.0.1:3031` if you reuse the old URI).
3. Get the consent URL:
   ```bash
   curl -s -H "Authorization: Bearer $KEY" http://localhost:3031/admin/youtube/auth
   ```
4. Open the `url` from the response, sign in with the channel's Google account and allow access. Google then redirects to `/admin/refreshtoken`, which stores the token. The `state` is HMAC-signed with the admin key and expires after 15 minutes.
5. Check it worked: `curl -s -H "Authorization: Bearer $KEY" http://localhost:3031/admin/youtube/status` should return `"valid": true`. The callback also runs this check and returns an error if the stored token can't be used.

### Keeping the token alive

The worker refreshes the access token on its own before every upload, but the **refresh token** can still stop working:

- **Six months without use.** Google revokes a refresh token that hasn't been used for six months. The old app only refreshed the token when it uploaded, so a channel that stopped streaming for six months lost it.
- **Testing mode.** While the OAuth consent screen is in "Testing", refresh tokens expire after 7 days. Set the app to "In production" (a personal app with the `youtube` scope works unverified; you just click through the "unverified app" warning once).
- **Revoked by hand**, a Google password change, or more than 100 refresh tokens issued for the same client and account.

To handle the first case and detect the others early, the worker has a keep-alive task. It runs when `ARCHIVE_YOUTUBE_UPLOAD` is on and a Google client is configured. It refreshes the token at startup and then every `ARCHIVE_YOUTUBE_KEEPALIVE_HOURS` (default 24). Each run logs one line:

- `YouTube token refreshed (keep-alive)`: all good.
- `ERROR ... YouTube token is not usable (RefreshError: invalid_grant ...)`: run Option B again before the next stream ends.

`GET /admin/youtube/status` does the same refresh on demand and returns `{"authorized": true, "valid": true, "accessTokenExpiry": "..."}`, or `{"authorized": ..., "valid": false, "error": "..."}`. `authorized` means a refresh token is stored. `valid` means Google accepted it just now.

---

## 4. Admin API

The admin API is served by the worker on port 3031. Keep it on your **private network**: never expose it through your reverse proxy or tunnel. Every call needs `Authorization: Bearer <ARCHIVE_ADMIN_API_KEY>`. The old app accepted any word before the key, and so does this one.

Long-running actions enqueue a job and return right away with `{"error": false, "msg": "...", "jobId": N}`. Errors return `{"error": true, "msg": "..."}`. Request bodies use the same field names as the old app. `platform` is accepted and ignored.

```bash
KEY=...           # ARCHIVE_ADMIN_API_KEY
A=http://your-server:3031
H=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")
```

### Jobs

```bash
curl -s "${H[@]}" "$A/admin/jobs"                       # latest 50 plus counts per state; filters below
curl -s "${H[@]}" "$A/admin/jobs?state=running,stopped" # states and/or groups, comma-separated
curl -s "${H[@]}" "$A/admin/jobs?kind=archive&vodId=...&limit=100"
curl -s "${H[@]}" "$A/admin/jobs/42"                    # one job: step, steps, attempts, lastError, payload
curl -s "${H[@]}" "$A/admin/kinds"                      # every job kind, its steps and its manual steps
curl -s "${H[@]}" -X POST "$A/admin/jobs" -d '{"kind":"download","vodId":"...","payload":{"type":"vod"}}'
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/pause"      # queued: now; running: when the current step ends
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/resume"     # run a paused job from its current step
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/resume" -d '{"once":true}'   # run one step, then pause again
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/retry"      # re-queue a failed job from the step it failed on
curl -s "${H[@]}" -X POST "$A/admin/jobs/42/cancel"     # queued/paused: now; running: stops mid-step
```

Job states are `queued`, `running`, `paused`, `done`, `failed` and `cancelled`. The `state` filter also takes groups: `waiting` (`queued`, including jobs waiting out a retry backoff), `stopped` (`paused`, `failed`, `cancelled`: they need you), `active` (`queued`, `running`, `paused`) and `finished` (`done`, `failed`, `cancelled`). A job whose step fails is retried after 2 minutes, then again after 4 minutes. On its third failure it is marked `failed` and keeps its files, so `retry` picks up from the same step. Cancelling a running job stops it immediately; the step's partial files stay on disk.

`POST /admin/jobs` starts any kind directly. Body fields: `kind` (required), `vodId`, `payload` (the same keys the specific routes put there, e.g. `type`, `stream_id`, `path`, `start_part`), `fromStep` (skip the steps before it), `pauseBefore` (this job's manual steps, see below) and `paused` (create it paused).

#### Manual steps

A job can stop before chosen steps and wait for you. `ARCHIVE_MANUAL_STEPS` sets them per kind for every job, including the ones the monitor starts, e.g. `{"archive":["upload"],"live":["upload"]}` archives and splits automatically but leaves every upload for you to approve. A job's own `pauseBefore` (from `POST /admin/jobs`) replaces the setting for that job; `[]` means no manual steps. The worker refuses to start if the setting names an unknown kind or step.

When a job reaches a manual step it becomes `paused` at that step. Look at it (`GET /admin/jobs?state=paused`), then `resume` it, `resume` with `{"once":true}` to run just that step, or `cancel` it. Steps that already finished are never re-run, so resuming continues exactly where it stopped.

Job kinds and their steps:

| Kind | Steps | Started by |
|---|---|---|
| `archive` | capture → finalize → chapters → chat → emotes → split → upload → describe → cleanup | monitor (stream went live), `/admin/hls/download` |
| `download` | ensure_source → chapters → split → upload → describe → cleanup | `/admin/download` |
| `reupload` | ensure_source → split → upload → describe → cleanup | `/admin/reupload` |
| `live` | live_record → resolve_vod → finalize → chapters → split → upload → describe → cleanup | monitor (when `LIVE_RECORD=true`) |
| `live_file` | ensure_source → chapters → split → upload → describe | `/v2/live` |
| `dmca` | ensure_source → dmca_edit → split → upload → describe → cleanup | `/admin/dmca` |
| `part_dmca` | ensure_source → split → dmca_edit → upload → describe → cleanup | `/admin/part/dmca` |
| `chat`, `logs_manual`, `chapters`, `emotes`, `describe` | one step each | the matching admin routes |

`ensure_source` uses `path` if one was given, otherwise the MP4 already on disk, otherwise it downloads the whole VOD from Twitch again (only while Twitch still has it).

### Recipes

**Backfill a VOD the monitor missed** (the VOD must still be on Twitch):

```bash
curl -s "${H[@]}" -X POST "$A/admin/hls/download" -d '{"vodId":"2375792832"}'
```

This creates the `vods` row from Helix if needed, then runs the full `archive` pipeline.

**Only create the database row** (chapters and emotes are included; no video):

```bash
curl -s "${H[@]}" -X POST "$A/admin/generate/vod" -d '{"vodId":"2375792832"}'
```

**Download and upload again** (all parts, or a range):

```bash
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832"}'
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832","startPart":2,"endPart":3}'
curl -s "${H[@]}" -X POST "$A/admin/download" -d '{"vodId":"2375792832","path":"/data/manual/2375792832.mp4"}'
```

**Re-upload one part** (e.g. YouTube processing failed):

```bash
curl -s "${H[@]}" -X POST "$A/admin/reupload" -d '{"vodId":"2375792832","part":2,"type":"vod"}'
```

The new video replaces the part's entry in `vods.youtube`. The old YouTube video is not deleted; remove it in YouTube Studio.

**Handle a copyright claim (DMCA):**

1. In YouTube Studio → Content → the video → Copyright, open DevTools → Network and reload. Find the Studio API response that contains `receivedClaims` (search the responses for that word) and copy the array. This is the same data the old app took. Each claim has `type`, `claimPolicy.primaryPolicy.policyType` and `matchDetails.longestMatchStartTimeSeconds/DurationSeconds`.
2. For **one part**, send the claims from that part's video. The timestamps are relative to that part:
   ```bash
   curl -s "${H[@]}" -X POST "$A/admin/part/dmca" \
     -d '{"vodId":"2375792832","part":2,"type":"vod","receivedClaims":[ ... ]}'
   ```
3. For a VOD uploaded as a **single video** (or claims timed against the full VOD):
   ```bash
   curl -s "${H[@]}" -X POST "$A/admin/dmca" -d '{"vodId":"2375792832","type":"vod","receivedClaims":[ ... ]}'
   ```

Only blocking policies (`GLOBAL_BLOCK`, `MOSTLY_GLOBAL_BLOCK`, `BLOCK`) are acted on. Audio claims are muted, visual claims are blacked out, and audiovisual claims get both. The edited video is uploaded as a new part.

**Chat, chapters, emotes, descriptions:**

```bash
curl -s "${H[@]}" -X POST "$A/admin/logs" -d '{"vodId":"..."}'                 # crawl chat again (resumes, skips duplicates)
curl -s "${H[@]}" -X POST "$A/admin/logs/manual" -d '{"vodId":"...","path":"/data/manual/chat.json"}'  # {"comments":{"edges":[...]}}
curl -s "${H[@]}" -X POST "$A/admin/chapters" -d '{"vodId":"..."}'
curl -s "${H[@]}" -X POST "$A/admin/emotes" -d '{"vodId":"..."}'
curl -s "${H[@]}" -X POST "$A/admin/duration" -d '{"vodId":"..."}'             # set duration from Helix
curl -s "${H[@]}" -X POST "$A/admin/youtube/parts" -d '{"vodId":"...","type":"vod"}'  # rewrite descriptions
```

`/admin/youtube/chapters` does the same as `/admin/youtube/parts`. Both rewrite every part's description from scratch: links to the other parts, the chat replay link, and the chapters inside that part.

**Create or delete rows by hand:**

```bash
curl -s "${H[@]}" -X POST "$A/admin/create" \
  -d '{"vodId":"123","title":"...","createdAt":"2026-01-01T20:00:00Z","duration":"03:00:00","platform":"twitch"}'
curl -s "${H[@]}" -X DELETE "$A/admin/delete" -d '{"vodId":"123"}'   # vod + logs + emotes + games
```

Paths in request bodies are **inside the worker container**, where `/data` is `ARCHIVE_HOST_DATA_DIR` on the host.

---

## 5. Live recordings and multiTrack

**Why record the live stream?** Twitch mutes copyrighted music in finished VODs, in 30-minute blocks (the `-muted` segments). A recording taken while the stream is live has the original audio. The downside is that anything that needed muting is now in your YouTube upload and may be claimed; the [DMCA recipes](#recipes) handle that.

**How it works (`ARCHIVE_LIVE_RECORD=true`):**

1. The monitor sees the channel go live and queues a `live` job for that stream ID.
2. `live_record` gets a live playback token and follows the source-quality playlist every 2 seconds. It writes segments to `/data/live/<streamId>/hls/`.
3. Twitch ad segments (stitched-ad `DATERANGE`s and `Amazon` segments) are skipped and turned into discontinuities. If the connection drops, the recorder reconnects with a new token.
4. The recording ends when the playlist ends, or when no new segments have appeared for about 3 minutes and Helix confirms the stream is over.
5. `resolve_vod` attaches the recording to the stream's VOD row. After that the job runs finalize → chapters → split → upload as `type: "live"`, with the title `… Twitch Live VOD - YYYY-MM-DD`.

**Which copies are uploaded:**

| `LIVE_RECORD` | `MULTI_TRACK` | Uploaded | `YOUTUBE_PUBLIC=true` makes public |
|---|---|---|---|
| false | – | VOD copy | the VOD copy |
| true | false | **live copy only** (the VOD is still downloaded for chat and chapters) | nothing (the live copy stays unlisted) |
| true | true | both | the live copy (the VOD copy stays unlisted) |

The frontend (`YoutubeVod.js`) plays `live` entries whenever a VOD has any, and falls back to `vod` entries otherwise. Chat sync works the same way for both.

**Disk usage:** the source quality is about 6–8 Mbit/s, which is roughly 3–3.5 GB per hour per copy. With both copies you need twice that until cleanup runs.

**External recorders (`POST /v2/live`)** still work. A recorder saves an MP4 and calls:

```bash
curl -s "${H[@]}" -X POST "$A/v2/live" -d '{"streamId":"318194011992","path":"/data/live/318194011992/rec.mp4"}'
```

That queues a `live_file` job. As with the old app, the call returns **404 when `MULTI_TRACK` is off**, which tells the recorder to delete its file. `driveId` is stored in `vods.drive` but nothing is uploaded to Drive.

---

## 6. Public API reference

This is what the frontend uses. The output is compatible with the old Feathers API. Tests replay 62 responses captured from the old API, and they match field for field (ignoring the added fields below).

| Route | Notes |
|---|---|
| `GET /vods` | Feathers envelope `{total, limit, skip, data}`. Each vod includes `games[]`. |
| `GET /vods/:id` | Single vod, or a 404 in the Feathers error format. |
| `GET /emotes?vod_id=` | `data[0]` has `ffz_emotes`, `bttv_emotes`, `7tv_emotes`. |
| `GET /games`, `/games/:id`, `/streams`, `/streams/:id`, `/emotes/:id` | Read-only. |
| `GET /v1/vods/:id/comments?content_offset_seconds=N` | Chat replay. 200-row buckets plus a `cursor`. |
| `GET /v1/vods/:id/comments?cursor=...` | Next page. |
| `GET /v2/badges` | `{channel: [...], global: [...]}` in the Helix `chat/badges` format, cached for 1h. |
| `GET /healthz` | `{"ok": true}` once the database responds. |

**Additions for the new sites.** The old API never had these; they only add routes and fields, so the old frontend is unaffected.

| Route / field | Notes |
|---|---|
| `GET /v1/games-played` | `[{name, gameId, image, imageTemplate, vods, chapters, lastPlayed}]`, one entry per game across all VODs' chapters. `vods` counts VODs (not chapters), `chapters` counts chapters, `lastPlayed` is the `createdAt` of the newest VOD with the game; `name`, `gameId` and `image` come from its most recent chapter. Grouped by `gameId`, else by name; chapters without a category are one entry named `No category` with `gameId: null`. Sorted by `vods` desc, `lastPlayed` desc, `name`. Cached like `/vods`. |
| `GET /v1/status` | `{live, stream, vod}`. Live: `stream` is `{id, started_at, title, game: {name, gameId, image, imageTemplate} \| null}` and `vod` is the VOD row of that stream (`null` until the worker has created it). Offline: `stream` is `null` and `vod` is the latest VOD. `vod` has the usual `/vods` fields. Live state comes from `streams.is_live`; title and category from Helix when credentials are set, else from the VOD's title and last chapter. Cached for 45 s. |
| `GET /v1/emotes/third-party` | `{"7tv": [...], "bttv": [...], "ffz": [...], "failed": [...]}`, each item `{id, code, provider}`: global plus channel emotes for `ARCHIVE_TWITCH_ID` from the providers' APIs (a channel emote replaces a global one with the same code). Build image URLs from the CDNs: `cdn.7tv.app/emote/{id}/1x.webp`, `cdn.betterttv.net/emote/{id}/1x`, `cdn.frankerfacez.com/emote/{id}/1`. A provider that failed is named in `failed` (its list holds whatever part loaded). Cached for 6 h, or 5 min when something failed. |
| `chapters[].imageTemplate` | On every chapter in `/vods` (and in `vod`s embedded elsewhere): the box art with `{width}x{height}` in place of the stored `40x53`, like Helix's `box_art_url`. `image` is unchanged. |
| `chapters[].length` | Same value as `end`, which holds the chapter's length in seconds, not its end time. |
| `duration_seconds` | On each VOD next to `duration` (`"HH:MM:SS"`), as a number. Only present when `duration` is. |

**Query syntax (Feathers):** `$limit`, `$skip`, `$sort[field]=1|-1` and `$select[]=field`. Field filters accept `$ne`, `$in`, `$nin`, `$lt`, `$lte`, `$gt`, `$gte`, `$like`, `$notLike`, `$iLike` and `$notILike`, and can be combined with `$or`/`$and`. `chapters[name]=text` does a case-insensitive substring match on chapter names. `chapters[name][$eq]=text` matches a chapter name exactly (case-sensitive), `chapters[gameId]=id` matches a chapter's gameId exactly, and `chapters[gameId]=null` finds VODs with an uncategorised chapter (the `No category` entry of `/v1/games-played`). Each matches when any chapter matches; several combine with AND. Unknown fields and filters on JSON columns return 400. POST, PUT, PATCH and DELETE return 405.

Examples: `/vods?$limit=20&$sort[createdAt]=-1`, `/vods?title[$iLike]=%25zelda%25`, `/vods?createdAt[$gte]=2025-01-01&createdAt[$lte]=2025-02-01`, `/vods?chapters[name]=Twilight`, `/vods?chapters[gameId]=368205`.

**Differences from the old API:** `$select` now works (it used to return a 500), `/v2/badges` now works, and the `chapters[name]` input is escaped and combines with other filters.

Rate limit: 20 requests per 5 s per IP on `/vods`, `/v1/*` and `/v2/*`. Responses carry `X-RateLimit-*` headers, and a request over the limit gets a 429.

---

## 7. Deployment (Docker Compose)

`compose.yaml` runs both services with host networking, next to a PostgreSQL that runs natively on the same host (`127.0.0.1:5432`). You need:

- Docker with the compose plugin.
- PostgreSQL 14 or later with the `archive` database. Restore the old app's database, or create an empty one and let the migrations build it.
- A data directory for the worker, set as `ARCHIVE_HOST_DATA_DIR` (default `./data`). A VOD needs about 3–3.5 GB per hour of stream while it is processed; twice that with `MULTI_TRACK`. Files are deleted after upload.
- A reverse proxy in front of the API port if the frontend is public. The worker's admin port must **not** go through it.

If the host is an unprivileged LXC container, Docker needs `nesting=1,keyctl=1`, and a bind-mounted data directory must be writable by the container's uid.

### 7.1 Install

1. **Back up the database** before touching the schema:
   ```bash
   sudo -u postgres pg_dump -Fc archive > ~/archive-$(date +%F).dump
   ```
2. **Get the code and configure it:**
   ```bash
   git clone https://github.com/vEXOULZ/twitch-archive.git && cd twitch-archive
   cp .env.example .env && $EDITOR .env
   mkdir -p secrets && cp deploy/secrets.example/*.env secrets/ && chmod 600 secrets/*.env && $EDITOR secrets/*.env
   docker compose build
   ```
3. **Migrate** as the superuser. This is safe on a live database: the baseline uses `CREATE TABLE IF NOT EXISTS`, and the `logs` indexes are built `CONCURRENTLY`. `secrets/admin.env` holds the superuser URL and the two role passwords. It is never mounted into a container.
   ```bash
   set -a && . secrets/admin.env && set +a
   docker compose run --rm -e ARCHIVE_MIGRATION_DATABASE_URL api alembic upgrade head
   ```
4. **Create the roles.** Re-run this after any migration that adds tables. As root on the database host:
   ```bash
   bash deploy/apply-roles.sh
   ```
   The passwords in `secrets/admin.env` must match the ones in the `ARCHIVE_DATABASE_URL` of `secrets/api.env` and `secrets/worker.env`. The script feeds `roles.sql` to `psql` on stdin, because the `postgres` user usually cannot read the checkout.
5. **Authorize YouTube** ([§3](#3-youtube-oauth-setup)).
6. **Start:** `docker compose up -d --wait`.

### 7.2 Migrating from the old Node app

1. **Run the new API next to the old one.** Set `ARCHIVE_API_PORT` to a free port (e.g. 3032), then `docker compose up -d api`. Check it: `curl -s 'http://127.0.0.1:3032/vods?$limit=1'`, and run the contract tests against it (§9).
2. **Switch the reverse proxy** to the new port. Open the site and check the list page, a VOD page, chat replay and badges.
3. **Switch the worker:** stop the old app, then `docker compose up -d worker`. Only one of them may run: both would archive the same streams. Watch `docker compose logs -f worker` through the next stream.
4. **Once you're confident:** move the API back to the old port if you like, disable the old app, and remove Redis. Nothing here uses it.

**Rollback** at any point: point the proxy back at the old app, `docker compose stop worker`, and start the old app. The schema changes are additive, so the old app keeps working. The new tables are `jobs` and `app_state`, which the old app ignores.

### 7.3 Updating

```bash
git pull
docker compose build && docker compose up -d --wait
# only if migrations/ changed: run 7.1 step 3 first (and step 4 if it added tables)
```

Running jobs are interrupted by the restart and resume from their current step. The two exceptions are a live recording, which misses the segments that aired during the restart, and an in-flight YouTube upload, which starts that part again from zero. Check `GET /admin/jobs?state=running` first.

Migrations must stay **additive**: the old containers keep running against the migrated schema until the new ones start, and a code rollback leaves the schema migrated.

Pushes and pull requests run CI (`.github/workflows/tests.yml`): the unit tests and both image builds. The database and contract tests skip there because they need a copy of a real database; run them locally (§9) before merging anything that touches queries.

---

## 8. Operations and troubleshooting

| Task | Command |
|---|---|
| Logs | `docker compose logs -f worker` / `docker compose logs -f api` |
| Status | `docker compose ps` (both services have healthchecks) |
| Failed jobs | `curl -s "${H[@]}" "$A/admin/jobs?state=failed"`, then `.../retry` |
| Queue a job without the API | `docker compose exec worker archive-worker enqueue chapters 2375792832` |
| Disk usage | `du -sh $ARCHIVE_HOST_DATA_DIR/*`; files are deleted after a successful upload unless `KEEP_*` is set. Failed jobs keep their files. |
| Stale work dirs | `$ARCHIVE_HOST_DATA_DIR/vods/<id>` with no job still active can be deleted by hand |

**Downloads fail with `PersistedQueryNotFound` or `No VOD playback token`.** Twitch rotated a GQL hash. Open twitch.tv in a browser, then DevTools → Network → filter `gql`. Play any VOD, find the `PlaybackAccessToken` request, and copy `extensions.persistedQuery.sha256Hash` into `ARCHIVE_GQL_HASH_PLAYBACK_TOKEN`. Do the same for `VideoCommentsByOffsetOrCursor` (open chat on a VOD), `VideoPreviewCard__VideoMoments` (the chapter list) and `NielsenContentMetadata`. Upstream ([TimIsOverpowered/archive](https://github.com/TimIsOverpowered/archive), `src/services/twitch/`) usually has the new values quickly. Then `docker compose up -d worker` and retry the failed jobs.

**Downloads fail with HTTP 403 on every variant.** Usher parameters changed or the VOD is sub-only. Compare `archive_worker/hls.py:vod_master_url` with upstream's `hls-utils.ts`.

**Chat crawl fails on `integrity` errors.** Twitch has started requiring client-integrity for the web client ID. Try another client ID for comments via `ARCHIVE_GQL_BACKUP_CLIENT_ID`.

**YouTube `invalid_grant`.** The refresh token is dead: it was revoked, unused for six months, or the OAuth app is in "Testing" mode (7-day tokens). The keep-alive task logs this within a day, and `/admin/youtube/status` shows it on demand. Run the consent flow again (§3, Option B), then retry the failed upload jobs; they resume with the parts not yet uploaded. See "Keeping the token alive" in §3.

**YouTube `uploadLimitExceeded` / `quotaExceeded`.** The daily limits reset at midnight Pacific time. The job fails after 3 attempts; retry it the next day and it resumes with the parts not yet uploaded.

**The API returns stale data after a worker update.** Responses are cached for `ARCHIVE_CACHE_TTL_SECONDS` (5 min). Chat cursor pages are cached for 24h, because finished VODs don't change.

---

## 9. Development

```
packages/common/archive_common/   settings, DB models, Twitch Helix/GQL clients, http helper
services/api/archive_api/         FastAPI app, Feathers query parser, serializers, comments port
services/worker/archive_worker/   monitor, job runner, steps/, hls, ffmpeg, youtube, admin API
migrations/                       Alembic (0000 legacy baseline, 0001 jobs/app_state/log indexes, 0002 jobs.not_before, 0003 manual step control)
tests/api_contract/               golden responses from the legacy API + replay tests
tests/worker/                     HLS parsing, planning, capture (respx), ffmpeg, DB-backed steps/runner
deploy/                           roles.sql, example secrets
docker/                           api.Dockerfile, worker.Dockerfile
```

```bash
uv run pytest                     # all tests; DB tests skip without Postgres, ffmpeg tests skip without ffmpeg
uv run pytest tests/worker -q
```

The DB-backed tests use `ARCHIVE_DATABASE_URL`. The default is the `compose.dev.yaml` database. They expect `alembic upgrade head` to have been run, and no other queued jobs in that database.

**Re-capturing the golden API responses** (only needed if the frontend starts using new queries). This reads from the old Node API:

```bash
uv run python tests/api_contract/capture_golden.py http://legacy-host:3030
```

Then restore a matching dump locally (`pg_dump -Fc archive` on the server, then `pg_restore` as in §1) so the replay compares against the same data.

**Conventions:**
- The runner saves `ctx.payload` after each step returns, so a finished step is never re-run. A step interrupted part-way is re-run, so long steps save their own progress in `ctx.payload` with `ctx.save()` and skip work already done.
- Every ffmpeg output is written to `*.part` and renamed on success.
- Pure logic lives in `planning.py` and `hls.py`, so it can be tested without I/O.
