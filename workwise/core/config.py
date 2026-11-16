
from ipaddress import ip_network
from typing import Literal

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
    # True behind a transaction pooler (Supabase port 6543): disables server-side prepared statements.
    db_pooler: bool = False


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
    api_ip_limit: int = Field(default=300, ge=10, le=100000)
    auth_reset_ip_limit: int = Field(default=10, ge=1, le=1000)
    # Password reset email. "disabled" refuses requests; "console" logs the message (development only).
    email_backend: Literal["disabled", "console", "smtp"] = "disabled"
    email_from: str = "no-reply@workwise.local"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True
    password_reset_url: str = "http://localhost:3000/reset-password"
    password_reset_minutes: int = Field(default=30, ge=5, le=120)
    # Resume uploads are stored as local files; the directory must be writable and persistent.
    resume_enabled: bool = True
    resume_dir: str = "data/resumes"
    resume_max_bytes: int = Field(default=2_000_000, ge=1_000, le=10_000_000)
    # Comma-separated CIDRs of reverse proxies whose X-Forwarded-For is believed. Empty = trust none.
    trusted_proxies: str = ""

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: str) -> str:
        for item in value.split(","):
            if item.strip():
                ip_network(item.strip())  # raises ValueError on a bad CIDR
        return value

    @field_validator("jwt_secret")
    @classmethod
    def reject_placeholder_secret(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().startswith("replace_"):
            raise ValueError("Generate a random JWT_SECRET before starting the API")
        return value
