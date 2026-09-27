# Host-only test runner for MahJourney backend.
#
# The project's intended test path is Docker/uv (see README). This helper runs
# the suite in a native Windows venv (.venv-win) for a fast TDD loop when the
# Docker daemon is not running. It forces synthetic data + test env so the app
# does not try to reach the compose "db" host.
#
# Two config tests (test_config_logging) assume a pristine environment with no
# .env present; on the host the repo .env leaks in, so they are expected to
# differ here. Run the full suite under Docker/uv for the authoritative result.
param([string]$Target = "")

$env:DATA_SOURCE = "synthetic"
$env:PERSISTENCE_ENABLED = "false"
$env:APP_ENV = "test"

& "$PSScriptRoot\.venv-win\Scripts\python.exe" -m pytest -q $Target
