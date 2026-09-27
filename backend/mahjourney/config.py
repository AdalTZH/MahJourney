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
    # --- Hands-free voice console (chained STT -> agent -> TTS pipeline) -------
    # The browser captures the mic, does voice-activity detection and
    # client-authoritative barge-in locally, and streams one finalized utterance
    # per turn to the /ws/voice WebSocket. The server transcribes it, runs the
    # SAME agent graph the text chat uses, then streams the spoken reply back
    # sentence-by-sentence. No speech-to-speech model is involved, so this is
    # far cheaper than the Realtime API while keeping full-duplex barge-in.
    voice_stt_model: str = "gpt-4o-mini-transcribe"
    voice_stt_language: str = "en"
    voice_tts_model: str = "gpt-4o-mini-tts"
    # Named voice for TTS (alloy, echo, fable, onyx, nova, shimmer, …).
    voice_tts_voice: str = "alloy"
    # Streaming-friendly, low-latency audio format the browser can play.
    voice_tts_format: str = "mp3"
    # Tone/accent/pace guidance (only honored by gpt-4o-mini-tts).
    voice_tts_instructions: str = (
        "Polite and warm. Calm and patient. Slightly formal but friendly. "
        "Clear pronunciation. Mild Singaporean English accent — neutral "
        "Singlish influence, not exaggerated. Medium pace, not rushed."
    )
    # Per-request timeout (seconds) applied to every OpenAI call. A dispatcher
    # turn chains several calls; without a cap a single stalled call blocks the
    # whole turn. On timeout the SDK raises and the caller falls back to its
    # deterministic path, so a slow model degrades gracefully instead of hanging.
    openai_request_timeout_seconds: float = 30.0
    lta_datamall_account_key: str = ""
    onemap_access_token: str = ""
    onemap_api_email: str = ""
    onemap_api_password: str = ""
    onemap_base_url: str = "https://www.onemap.gov.sg"
    data_gov_sg_api_key: str = ""
    nea_weather_base_url: str = "https://api-open.data.gov.sg/v2/real-time/api"
    graphhopper_api_key: str = ""
    graphhopper_base_url: str = "https://graphhopper.com/api/1"
    # Routing profile used for reroute geometry. Must match a profile declared
    # in graphhopper/config.yml. Defaults to "van" to match the delivery fleet.
    # NOTE: the hosted GraphHopper API only supports its own built-in profiles
    # (car, truck, small_truck, ...) — it does NOT know "van". Set this to
    # "car" (or "small_truck" on a paid hosted plan) if you point at the hosted
    # API instead of the self-hosted container.
    graphhopper_profile: str = "van"
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    database_url: str = "postgresql+asyncpg://mahjourney:mahjourney@db:5432/mahjourney"
    persistence_enabled: bool = False
    max_stops_per_vehicle: int = 25
    # When false, delivery time windows are ignored during planning: every
    # order can be served any time within the assigned vehicle's working
    # hours (early or late), instead of being confined to its own
    # window_start_minute/window_end_minute. Capacity, working-hours, and
    # max-stops constraints are unaffected.
    enforce_delivery_windows: bool = True
    # When true (and a OneMap token is configured), stop visit order within each
    # vehicle's route is optimized on real OneMap road distances instead of
    # straight-line distance. Adds OneMap calls at plan-generation time, so it
    # is best suited to small/demo fleets.
    road_optimized_routing: bool = False
    app_session_secret: str = "development-only-session-secret"
    # Single shared dispatcher/admin login. admin_password_hash is a
    # "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>" string produced by
    # mahjourney.auth.hash_password — never store a plaintext password here.
    admin_username: str = "admin"
    admin_password_hash: str = ""
    session_ttl_minutes: int = 720
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
    # Cap on how many auto-generated INCIDENT_LESSON proposals are recalled into
    # a single router prompt. These are UNVETTED (PROPOSED/UNTRUSTED_EXTERNAL)
    # worker warnings fed back without human curation, so the cap bounds both the
    # token cost and the noise/signal risk. Tune once real volume is observed;
    # recall takes the most RECENT matching lessons up to this many.
    incident_lesson_recall_limit: int = 5
    # Age-based pruning for MasterMemory, mirroring conversation_retention_days'
    # discipline. MemoryItem has no expires_at column (unlike
    # ConversationMessage), so pruning is computed from the existing created_at
    # field at prune time rather than a stored expiry. Two independent windows:
    # uncurated auto-lessons are pruned aggressively since nobody has acted on
    # them; superseded items (dead to every recall path already) are kept
    # longer only in case their supersedes_id provenance is ever needed.
    proposed_incident_lesson_retention_days: int = 14
    superseded_memory_retention_days: int = 90

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
        invalid_prefixes = ("development-only", "replace-with", "change-me")
        if any(
            len(value := getattr(self, field)) < 32
            or value.lower().startswith(invalid_prefixes)
            for field in secret_fields
        ):
            raise ValueError("production requires generated application secrets")
        if not self.persistence_enabled:
            raise ValueError("production requires PostgreSQL persistence")
        if not self.admin_password_hash:
            raise ValueError("production requires ADMIN_PASSWORD_HASH to be set")
        return self

    @property
    def database_required(self) -> bool:
        """A live DB connection is always required: operational data (fleet,
        orders, depots) is sourced exclusively from PostgreSQL — there is no
        synthetic/in-memory fallback."""
        return True

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
        missing = []
        if not self.lta_datamall_account_key:
            missing.append("LTA_DATAMALL_ACCOUNT_KEY")
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        # OneMap requires either a static token OR email+password for auto-refresh.
        has_onemap = bool(
            self.onemap_access_token
            or (self.onemap_api_email and self.onemap_api_password)
        )
        if not has_onemap:
            missing.append("ONEMAP_ACCESS_TOKEN (or ONEMAP_API_EMAIL + ONEMAP_API_PASSWORD)")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
