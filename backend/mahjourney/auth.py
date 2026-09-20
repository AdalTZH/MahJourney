"""Admin login: password hashing and signed session tokens.

This app has a single shared admin/dispatcher credential (no per-user table —
see the investigation in the PR description for why a full user table was
judged unnecessary at this scale). The password is never stored in plaintext;
only a salted PBKDF2 hash lives in configuration. A successful login gets a
signed, expiring token in an HttpOnly cookie; every other route requires that
cookie to verify.

Uses only the standard library (hashlib/hmac/secrets) plus the HMAC-signing
pattern already used elsewhere in this codebase (security.secure_digest), so
no new dependency is introduced for something this small.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from .security import secure_digest

_PBKDF2_ITERATIONS = 260_000
_ALGORITHM = "pbkdf2_sha256"

SESSION_COOKIE_NAME = "mahjourney_session"


def hash_password(password: str) -> str:
    """Produce a storable hash: "pbkdf2_sha256$<iterations>$<salt>$<hash>"."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS
    ).hex()
    return f"{_ALGORITHM}${_PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check of ``password`` against a hash from ``hash_password``.

    Returns False (never raises) for a malformed or empty stored hash, so a
    misconfigured or unset ADMIN_PASSWORD_HASH simply rejects every login
    instead of crashing the request.
    """
    try:
        algorithm, iterations_text, salt, expected_hex = stored_hash.split("$")
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    try:
        iterations = int(iterations_text)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return hmac.compare_digest(candidate, expected_hex)


def issue_session_token(username: str, secret: str, ttl_minutes: int) -> str:
    """Build a signed "username.expiry.signature" token for the session cookie."""
    expires_at = int((datetime.now(UTC) + timedelta(minutes=ttl_minutes)).timestamp())
    body = f"{username}.{expires_at}"
    signature = secure_digest(body, secret)
    return f"{body}.{signature}"


class LoginThrottle:
    """In-memory brute-force slowdown for the login endpoint.

    Not a substitute for a real rate limiter at the edge (e.g. Caddy/WAF) —
    this only protects a single API process's login route from being hammered
    directly, tracking failures per source IP so one attacker can't lock out
    a legitimate admin from a different address.
    """

    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 60) -> None:
        self._max_attempts = max_attempts
        self._lockout = timedelta(seconds=lockout_seconds)
        self._failures: dict[str, list[datetime]] = {}

    def is_locked(self, key: str) -> bool:
        cutoff = datetime.now(UTC) - self._lockout
        attempts = [when for when in self._failures.get(key, []) if when > cutoff]
        self._failures[key] = attempts
        return len(attempts) >= self._max_attempts

    def record_failure(self, key: str) -> None:
        self._failures.setdefault(key, []).append(datetime.now(UTC))

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)


def verify_session_token(token: str, secret: str) -> str | None:
    """Return the username if ``token`` is a valid, unexpired session token."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    username, expires_at_text, signature = parts
    body = f"{username}.{expires_at_text}"
    if not hmac.compare_digest(signature, secure_digest(body, secret)):
        return None
    try:
        expires_at = int(expires_at_text)
    except ValueError:
        return None
    if expires_at < int(datetime.now(UTC).timestamp()):
        return None
    return username
