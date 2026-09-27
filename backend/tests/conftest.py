"""Shared pytest configuration for the MahJourney backend suite.

This module runs at collection time — before any test imports
``mahjourney.config`` — so environment defaults set here are in place by the
time ``Settings``/``get_settings()`` is first constructed.

The key one is ``ORTOOLS_TIME_LIMIT_SECONDS``. In production the OR-Tools
route search runs with a multi-second budget so GUIDED_LOCAL_SEARCH can improve
on the greedy first solution. GLS runs until that wall clock expires, so the
production budget makes the plan-building tests slow (the scenario/disruption
suites build many plans). Tests only need the solver to run and return a valid
route, not to be finely optimized, so we pin the budget to 1 second here to keep
CI fast. ``setdefault`` means an explicit override still wins — e.g. run
``ORTOOLS_TIME_LIMIT_SECONDS=5 pytest ...`` to exercise the production budget.

Only the OR-Tools budget is pinned here. The DATA_SOURCE/PERSISTENCE_ENABLED/
APP_ENV defaults are deliberately left to the test runner (``run-host-tests.ps1``
or the Docker/uv harness) rather than set here, because ``test_config_logging``
asserts ``Settings`` behavior against a pristine environment (e.g. that
``app_env`` defaults to ``development``); forcing ``APP_ENV=test`` from a
blanket conftest would break those pristine-env assertions in an otherwise clean
``pytest`` run.
"""

from __future__ import annotations

import os

os.environ.setdefault("ORTOOLS_TIME_LIMIT_SECONDS", "1")
