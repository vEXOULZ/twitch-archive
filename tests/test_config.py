from pathlib import Path

import pytest
from archive_common.config import Settings


def load(secrets_dir: Path) -> Settings:
    # pydantic-settings' init-only arguments, which mypy does not see without the pydantic plugin
    return Settings(_env_file=None, _secrets_dir=secrets_dir)  # type: ignore[call-arg]


def test_secrets_come_from_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVE_ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("ARCHIVE_DATABASE_URL", raising=False)
    (tmp_path / "archive_admin_api_key").write_text("from-a-file\n")
    (tmp_path / "archive_database_url").write_text("postgresql+asyncpg://u:p@db/archive\n")
    settings = load(tmp_path)
    assert settings.admin_api_key.get_secret_value() == "from-a-file"
    assert settings.database_url == "postgresql+asyncpg://u:p@db/archive"


def test_an_empty_file_leaves_a_setting_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARCHIVE_ADMIN_PASSWORD", raising=False)
    (tmp_path / "archive_admin_password").write_text("")
    settings = load(tmp_path)
    assert settings.admin_password.get_secret_value() == ""


def test_environment_wins_over_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHIVE_ADMIN_API_KEY", "from-the-environment")
    (tmp_path / "archive_admin_api_key").write_text("from-a-file")
    settings = load(tmp_path)
    assert settings.admin_api_key.get_secret_value() == "from-the-environment"
