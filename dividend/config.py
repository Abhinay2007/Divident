"""Validated application settings, independent of provider implementations."""

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "data" / "allocation_policy.yaml"


class Settings(BaseSettings):
    """Environment-backed configuration for the Dividend application."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", validate_default=True)

    ADMIN_TOKEN: str = Field(min_length=1)
    PUBLIC_BASE_URL: AnyHttpUrl
    DATABASE_URL: str = "sqlite:///./dividend.db"
    CORS_ORIGINS: list[AnyHttpUrl] = Field(default_factory=lambda: ["http://localhost:5173"])
    DEMO_SEED_ON_BOOT: bool = True
    DEMO_SANDBOX_RECIPIENTS_FILE: Path | None = Field(default=None, exclude=True, repr=False)

    PAYPAL_ENV: Literal["sandbox"] = "sandbox"
    PAYMENTS_MODE: Literal["paypal", "fake"] = "paypal"
    PAYPAL_CLIENT_ID: str | None = None
    PAYPAL_CLIENT_SECRET: str | None = Field(default=None, repr=False)

    MODAL_ENDPOINT_URL: AnyHttpUrl = "https://asthorat7-pune--ep-deepseek-v4-1-flash-1017-server.us-west.modal.direct/v1"
    MODAL_MODEL: str = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
    MODAL_PROXY_TOKEN_ID: str | None = Field(default=None, repr=False)
    MODAL_PROXY_TOKEN_SECRET: str | None = Field(default=None, repr=False)
    LLM_LIVE_ENABLED: bool = True
    LLM_KILL_SWITCH: bool = False

    ENRICH_MODE: Literal["fixtures", "live"] = "fixtures"
    GITHUB_TOKEN: str | None = Field(default=None, repr=False)
    GITHUB_OAUTH_CLIENT_ID: str | None = None
    GITHUB_OAUTH_CLIENT_SECRET: str | None = Field(default=None, repr=False)
    UDD_DSN: str | None = Field(default=None, repr=False)

    allocation_policy_path: Path = Field(default=DEFAULT_POLICY_PATH, exclude=True, repr=False)

    @field_validator("ADMIN_TOKEN")
    @classmethod
    def admin_token_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("ADMIN_TOKEN must not be blank")
        return value

    @field_validator("DATABASE_URL")
    @classmethod
    def database_url_must_be_valid(cls, value: str) -> str:
        parsed = urlsplit(value)
        if not parsed.scheme or (parsed.scheme not in {"sqlite", "postgresql", "postgresql+psycopg"}):
            raise ValueError("DATABASE_URL must use a supported SQLite or PostgreSQL URL")
        if parsed.scheme == "sqlite" and not (parsed.path or parsed.netloc):
            raise ValueError("DATABASE_URL must identify a SQLite database")
        if parsed.scheme.startswith("postgresql") and (not parsed.hostname or not parsed.path.strip("/")):
            raise ValueError("DATABASE_URL must identify a PostgreSQL host and database")
        return value

    @model_validator(mode="after")
    def validate_mode_credentials(self):
        if bool(self.GITHUB_OAUTH_CLIENT_ID) != bool(self.GITHUB_OAUTH_CLIENT_SECRET):
            raise ValueError("GitHub maintainer claims require both GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET")
        if self.PAYMENTS_MODE == "paypal":
            missing = [name for name, value in (
                ("PAYPAL_CLIENT_ID", self.PAYPAL_CLIENT_ID),
                ("PAYPAL_CLIENT_SECRET", self.PAYPAL_CLIENT_SECRET),
            ) if not value or not value.strip()]
            if missing:
                raise ValueError("PayPal mode requires: " + ", ".join(missing))
        if bool(self.MODAL_PROXY_TOKEN_ID) != bool(self.MODAL_PROXY_TOKEN_SECRET):
            raise ValueError("Modal proxy authentication requires both token ID and secret")
        if self.LLM_LIVE_ENABLED and not self.LLM_KILL_SWITCH and not (
            self.MODAL_PROXY_TOKEN_ID and self.MODAL_PROXY_TOKEN_SECRET
        ):
            raise ValueError("MODAL_PROXY_TOKEN_ID and MODAL_PROXY_TOKEN_SECRET are required when LLM is enabled")
        if self.ENRICH_MODE == "live":
            missing = [name for name, value in (
                ("GITHUB_TOKEN", self.GITHUB_TOKEN), ("UDD_DSN", self.UDD_DSN),
            ) if not value]
            if missing:
                raise ValueError("Live enrichment requires: " + ", ".join(missing))
        return self


def get_settings(**overrides) -> Settings:
    """Construct settings; keyword overrides support isolated tests and callers."""
    return Settings(**overrides)
