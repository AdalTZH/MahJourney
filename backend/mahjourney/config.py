from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_timezone: str = "Asia/Singapore"
    app_public_url: str = "http://localhost:3000"
    cors_allowed_origins: str = "http://localhost:3000"
    log_level: str = "INFO"

    openai_api_key: str = ""
    openai_model: str = "gpt-5.4-mini-2026-03-17"
    openai_embedding_model: str = "text-embedding-3-small"
    lta_datamall_account_key: str = ""
    onemap_access_token: str = ""
    onemap_base_url: str = "https://www.onemap.gov.sg"
    data_gov_sg_api_key: str = ""
    nea_weather_base_url: str = "https://api-open.data.gov.sg/v2/real-time/api"
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    database_url: str = "postgresql+asyncpg://mahjourney:mahjourney@db:5432/mahjourney"
    persistence_enabled: bool = False
    app_session_secret: str = "development-only-session-secret"
    telegram_webhook_secret: str = "development-only-webhook-secret"
    enrollment_token_pepper: str = "development-only-enrollment-pepper"
    audit_chain_hmac_key: str = "development-only-audit-key"
    approval_action_hmac_key: str = "development-only-approval-key"

    x401_issuer_private_key_path: str = "/run/secrets/x401_issuer_private.jwk"
    x401_protocol_version: str = "0.1.0"
    x401_trusted_issuer: str = "mahjourney-demo-issuer"
    x401_verifier_audience: str = "mahjourney-dispatcher"

    lta_incidents_poll_seconds: int = 120
    lta_vms_poll_seconds: int = 120
    lta_speed_bands_poll_seconds: int = 300
    lta_travel_times_poll_seconds: int = 300
    lta_incidents_stale_seconds: int = 360
    lta_traffic_stale_seconds: int = 900
    nea_rainfall_poll_seconds: int = 300
    nea_two_hour_forecast_poll_seconds: int = 1800
    nea_rainfall_stale_seconds: int = 900
    nea_forecast_stale_seconds: int = 3600
    markov_min_conditional_transitions: int = 100
    ortools_time_limit_seconds: int = 5
    monte_carlo_online_samples: int = 1000
    monte_carlo_eval_samples: int = 5000
    conversation_retention_days: int = 30

    live_external_read_tests: bool = True
    telegram_send_tests: bool = False
    allow_plan_execution_in_tests: bool = False
    bert_guard_enabled: bool = False
    mascot_enabled: bool = False

    @field_validator("bert_guard_enabled", "mascot_enabled")
    @classmethod
    def excluded_features_stay_disabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("BERT runtime and mascot are outside the approved scope")
        return value

    @model_validator(mode="after")
    def production_secrets_are_not_defaults(self) -> "Settings":
        if self.app_env != "production":
            return self
        secret_fields = (
            "app_session_secret",
            "telegram_webhook_secret",
            "enrollment_token_pepper",
            "audit_chain_hmac_key",
            "approval_action_hmac_key",
        )
        if any(getattr(self, field).startswith("development-only") for field in secret_fields):
            raise ValueError("production requires generated application secrets")
        if not self.persistence_enabled:
            raise ValueError("production requires PostgreSQL persistence")
        return self

    @property
    def cors_origins(self) -> list[str]:
        origins = [
            origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()
        ]
        if self.app_env == "development":
            origins.extend(
                origin
                for origin in ("http://localhost:3000", "http://127.0.0.1:3000")
                if origin not in origins
            )
        return origins

    def missing_live_credentials(self) -> list[str]:
        values = {
            "OPENAI_API_KEY": self.openai_api_key,
            "LTA_DATAMALL_ACCOUNT_KEY": self.lta_datamall_account_key,
            "ONEMAP_ACCESS_TOKEN": self.onemap_access_token,
        }
        return [key for key, value in values.items() if not value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
