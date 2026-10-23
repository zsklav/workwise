
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from workwise.core.paths import ROOT_DIR


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
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


class AuthSettings(Settings):
    jwt_secret: SecretStr = Field(min_length=32)
    jwt_issuer: str = "workwise"
    jwt_audience: str = "workwise-api"
    access_token_expire_minutes: int = Field(default=30, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=30)
    auth_rate_window_seconds: int = Field(default=60, ge=10, le=3600)
    auth_login_ip_limit: int = Field(default=30, ge=1, le=1000)
    auth_login_account_limit: int = Field(default=10, ge=1, le=100)
    auth_signup_ip_limit: int = Field(default=20, ge=1, le=100)
    auth_refresh_ip_limit: int = Field(default=60, ge=1, le=1000)

    @field_validator("jwt_secret")
    @classmethod
    def reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().startswith("replace_"):
            raise ValueError("Generate a random JWT_SECRET before starting the API")
        return value
