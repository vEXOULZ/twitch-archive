"""Settings shared by archive-api and archive-worker.

Everything comes from environment variables prefixed ``ARCHIVE_`` (or a
``.env`` file in the working directory). List values are JSON, e.g.
``ARCHIVE_RESTRICTED_GAMES='["Artifact"]'``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARCHIVE_", env_file=".env", extra="ignore")

    # ── Shared ────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:dev@127.0.0.1:55433/archive"
    channel: str = ""  # display name in YouTube titles
    domain_name: str = ""  # frontend host for the "Chat Replay" link
    timezone: str = "UTC"
    log_level: str = "INFO"

    # ── Twitch ────────────────────────────────────────────────────────────
    twitch_id: str = ""
    twitch_username: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = SecretStr("")

    # Twitch private GQL: these rotate from time to time. When downloads start
    # failing with "PersistedQueryNotFound", update the hash here.
    gql_client_id: str = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    gql_backup_client_id: str = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"
    gql_hash_playback_token: str = "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9"
    gql_hash_comments: str = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"
    gql_hash_moments: str = "7399051b2d46f528d5f0eedf8b0db8d485bb1bb4c0a2c6707be6f1290cdcb31a"
    gql_hash_nielsen: str = "2dbf505ee929438369e68e72319d1106bb3c142e295332fac157c90638968586"

    # ── API ───────────────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 3030
    paginate_default: int = 10
    paginate_max: int = 50
    rate_limit_points: int = 20
    rate_limit_window_seconds: int = 5
    cache_ttl_seconds: int = 300

    # ── Worker ────────────────────────────────────────────────────────────
    data_dir: Path = Path("/data")
    vod_download: bool = True
    chat_download: bool = True
    live_record: bool = False
    multi_track: bool = False
    youtube_upload: bool = True
    youtube_public: bool = False
    youtube_description: str = "VOD"
    youtube_keepalive_hours: float = 24.0  # periodic token refresh; see README "YouTube OAuth"
    restricted_games: list[str] = []
    # Steps a job pauses before until resumed (POST /admin/jobs/{id}/resume), per
    # job kind, e.g. {"archive": ["upload"]}. A job's own pauseBefore overrides it.
    # Kinds and steps: GET /admin/kinds, or jobs.KINDS in the worker.
    manual_steps: dict[str, list[str]] = {}
    split_duration: int = 10800
    keep_hls: bool = False
    keep_mp4: bool = False
    dry_run: bool = False

    monitor_interval_seconds: int = 30
    hls_poll_interval_seconds: int = 60
    hls_no_change_threshold: int = 10
    live_poll_interval_seconds: float = 2.0
    live_end_threshold: int = 90  # consecutive polls without new segments (~3 min)
    segment_concurrency: int = 4

    admin_host: str = "0.0.0.0"
    admin_port: int = 3031
    admin_api_key: SecretStr = SecretStr("")
    # Browser (dashboard) login: unset turns password login off; the API key keeps working.
    admin_password: SecretStr = SecretStr("")
    # Addresses (or CIDR ranges) of reverse proxies whose X-Forwarded-For / X-Real-IP is
    # believed when rate-limiting logins. Empty: always use the connecting address.
    admin_trusted_proxies: list[str] = []
    # Where /admin/health looks for archive-api; empty = http://127.0.0.1:<api_port>.
    api_internal_url: str = ""

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    google_redirect_url: str = "http://localhost:3031/admin/refreshtoken"

    @property
    def vod_dir(self) -> Path:
        return self.data_dir / "vods"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"


@lru_cache
def get_settings() -> Settings:
    return Settings()
