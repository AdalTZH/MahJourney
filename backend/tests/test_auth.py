from mahjourney.auth import (
    LoginThrottle,
    hash_password,
    issue_session_token,
    verify_password,
    verify_session_token,
)


def test_hash_password_round_trips_and_rejects_wrong_password() -> None:
    stored = hash_password("correct-password")
    assert verify_password("correct-password", stored) is True
    assert verify_password("wrong-password", stored) is False


def test_hash_password_uses_a_fresh_salt_each_time() -> None:
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_verify_password_rejects_malformed_or_empty_hash() -> None:
    assert verify_password("anything", "") is False
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "pbkdf2_sha256$not-an-int$salt$hash") is False
    assert verify_password("anything", "md5$1000$salt$hash") is False


def test_session_token_round_trips_and_rejects_tampering() -> None:
    token = issue_session_token("admin", "secret", ttl_minutes=10)
    assert verify_session_token(token, "secret") == "admin"
    assert verify_session_token(token, "different-secret") is None
    tampered = token.rsplit(".", 1)[0] + ".tampered-signature"
    assert verify_session_token(tampered, "secret") is None


def test_session_token_rejects_expired_token() -> None:
    token = issue_session_token("admin", "secret", ttl_minutes=-1)
    assert verify_session_token(token, "secret") is None


def test_session_token_rejects_malformed_token() -> None:
    assert verify_session_token("not.enough.parts.here", "secret") is None
    assert verify_session_token("", "secret") is None


def test_login_throttle_locks_out_after_max_attempts_and_clears() -> None:
    throttle = LoginThrottle(max_attempts=3, lockout_seconds=60)
    assert throttle.is_locked("1.2.3.4") is False
    for _ in range(3):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_locked("1.2.3.4") is True
    # A different key (different source IP) is unaffected.
    assert throttle.is_locked("5.6.7.8") is False
    throttle.clear("1.2.3.4")
    assert throttle.is_locked("1.2.3.4") is False
