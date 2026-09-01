"""Environment configuration. Production starts with no authority to write."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "Spiral Max SEO"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite:///./seo-autonomous.db"
    autonomy_level: int = Field(default=1, ge=0, le=5)
    production_enabled: bool = False
    shadow_mode: bool = True
    api_token: SecretStr | None = None
    approval_token: SecretStr | None = None
    admin_token: SecretStr | None = None
    log_level: str = "INFO"
    scheduler_enabled: bool = False
    scheduler_timezone: str = "UTC"
    scheduler_lease_seconds: int = Field(default=300, ge=30, le=3600)
    cors_origins: list[str] = Field(default_factory=list)
    max_daily_actions: int = Field(default=5, ge=0, le=100)
    max_agent_turns: int = Field(default=4, ge=1, le=12)
    max_agent_calls_per_run: int = Field(default=8, ge=1, le=30)
    max_daily_cost_usd: float = Field(default=5.0, ge=0, allow_inf_nan=False)
    max_crawl_pages: int = Field(default=100, ge=1, le=10000)
    crawl_timeout_seconds: float = Field(default=15, gt=0, le=60)
    crawl_max_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
    crawl_user_agent: str = "SpiralMaxSEO/0.1 (+site-owner-controlled-audit)"
    openai_api_key: SecretStr | None = None
    openai_model: str | None = None
    provider_mode: Literal["fixture", "live"] = "fixture"
    agent_mode: Literal["fixture", "deterministic", "openai"] = "fixture"
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "testserver"])
    max_pages_per_crawl: int = Field(default=50, ge=1, le=10000)
    google_application_credentials: str | None = None
    gsc_property: str | None = None
    ga4_property_id: str | None = None
    wordpress_url: str | None = None
    wordpress_username: str | None = None
    wordpress_application_password: SecretStr | None = None
    dataforseo_login: str | None = None
    dataforseo_password: SecretStr | None = None
    github_token: SecretStr | None = None
    github_repository: str | None = None
    otel_service_name: str = "seo-control-plane"
    otel_exporter_otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def validate_authority(self) -> "Settings":
        tokens = [t.get_secret_value() for t in (self.api_token, self.approval_token, self.admin_token) if t]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Agent, approval, and administrator tokens must be distinct capabilities")
        if self.production_enabled and self.shadow_mode:
            raise ValueError("Production execution cannot be enabled while shadow mode is active")
        if self.autonomy_level > 2:
            raise ValueError("This release supports autonomy levels 0–2 only; graduation is not automatic")
        if self.agent_mode == "openai" and (not self.openai_api_key or not self.openai_model):
            raise ValueError("Live agents require OPENAI_API_KEY and an explicitly selected OPENAI_MODEL")
        if self.environment == "production":
            if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
                raise ValueError("Production requires PostgreSQL")
            if not self.api_token or len(self.api_token.get_secret_value()) < 32:
                raise ValueError("Production requires an API token of at least 32 characters")
            for name, token in (("API", self.api_token), ("approval", self.approval_token), ("administrator", self.admin_token)):
                if token is not None and len(token.get_secret_value()) < 32:
                    raise ValueError(f"Production {name} tokens must contain at least 32 characters")
            if self.production_enabled and (not self.approval_token or len(self.approval_token.get_secret_value()) < 32):
                raise ValueError("Production mutations require a separate approval token of at least 32 characters")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
