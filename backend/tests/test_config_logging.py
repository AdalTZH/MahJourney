import logging

import pytest
from pydantic import ValidationError

from mahjourney.config import Settings
from mahjourney.main import SecretRedactionFilter


def test_development_cors_always_includes_local_preview() -> None:
    settings = Settings(_env_file=None, cors_allowed_origins="https://tunnel.example")
    assert settings.cors_origins == [
        "https://tunnel.example",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_production_rejects_default_secrets() -> None:
    with pytest.raises(ValidationError, match="production requires generated application secrets"):
        Settings(_env_file=None, app_env="production")


def test_secret_filter_redacts_supported_credentials() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "Authorization=Bearer-secret AccountKey:abc API_KEY=xyz password=hunter2",
        (),
        None,
    )
    assert SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "Bearer-secret" not in rendered
    assert "abc" not in rendered
    assert "xyz" not in rendered
    assert "hunter2" not in rendered
