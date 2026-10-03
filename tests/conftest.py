"""Pytest entry point: the shared import bootstrap, plus a clear failure when the runtime is missing.

The path plumbing lives in ``_paths.py`` so the standalone (pytest-less) runners use the same one.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _paths  # noqa: E402


def pytest_configure(config) -> None:
    if _paths.RUNTIME is None:
        raise pytest.UsageError(_paths.MISSING_RUNTIME_HINT)
