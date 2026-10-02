"""Make the plugin's imports work from any checkout, on any machine.

``pytest.ini`` puts the plugin's parent on ``sys.path`` — that is what makes ``from vk.…`` resolve. The
Hermes runtime behind ``from gateway.…`` cannot be hard-coded in a published repo, so it is located here:
``$HERMES_INSTALL`` first, then the standard layout (``<hermes>/hermes-agent`` beside this plugin's tree),
then the working directory. Run from anywhere; the directory is found or the run fails with instructions.
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve()


def hermes_install() -> pathlib.Path | None:
    """The Hermes runtime directory — the one containing ``gateway/``."""
    candidates = []
    if os.environ.get("HERMES_INSTALL"):
        candidates.append(pathlib.Path(os.environ["HERMES_INSTALL"]).expanduser())
    parents = _HERE.parents
    if len(parents) > 4:  # <hermes>/plugins/platforms/<plugin>/tests/conftest.py -> <hermes>/hermes-agent
        candidates.append(parents[4] / "hermes-agent")
    candidates += [pathlib.Path.cwd(), pathlib.Path.cwd() / "hermes-agent"]
    return next((c.resolve() for c in candidates if (c / "gateway").is_dir()), None)


_RUNTIME = hermes_install()
if _RUNTIME is not None and str(_RUNTIME) not in sys.path:
    sys.path.insert(0, str(_RUNTIME))


def pytest_configure(config) -> None:
    if _RUNTIME is None:
        raise pytest.UsageError(
            "Hermes runtime not found — the tests import `gateway.*` from the Hermes install.\n"
            "Set HERMES_INSTALL=/path/to/hermes-agent (the directory that contains gateway/) and re-run."
        )
