from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().with_name(".env"),
        env_file_encoding="utf-8",
        hide_input_in_errors=True,
        extra="ignore",
    )

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "workwise"
    db_user: str = "workwise_app"
    db_password: SecretStr


class MigrationSettings(Settings):
    migration_db_user: str = "workwise_migrator"
    migration_db_password: SecretStr
