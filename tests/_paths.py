"""Import bootstrap for the test suite — the checkout directory's name must not matter.

The plugin is imported as ``vk`` (``from vk.adapter import …``), but a clone lands in a directory
named after the repository (``hermes-vk``), which is not a valid module name and is not what the
imports expect. Registering the directory as the ``vk`` package here decouples the two, so the suite
runs from any checkout name.

It also locates the Hermes runtime behind ``from gateway.…``: ``$HERMES_INSTALL``, the standard layout
(``~/.hermes/plugins/platforms/<plugin>`` → ``~/.hermes/hermes-agent``), or the working directory.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve()
PLUGIN_DIR = _HERE.parents[1]  # tests/_paths.py -> the plugin directory

MISSING_RUNTIME_HINT = (
    "Hermes runtime not found — the tests import `gateway.*` from the Hermes install.\n"
    "Set HERMES_INSTALL=/path/to/hermes-agent (the directory that contains gateway/) and re-run."
)


def _register_plugin_package() -> None:
    """Expose the plugin directory as the package ``vk``, whatever it is called on disk."""
    if "vk" in sys.modules or not (PLUGIN_DIR / "__init__.py").is_file():
        return
    spec = importlib.util.spec_from_file_location(
        "vk", PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - unreadable __init__.py
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["vk"] = module
    spec.loader.exec_module(module)


def _find_runtime() -> pathlib.Path | None:
    """The Hermes runtime directory — the one containing ``gateway/``."""
    candidates = []
    if os.environ.get("HERMES_INSTALL"):
        candidates.append(pathlib.Path(os.environ["HERMES_INSTALL"]).expanduser())
    parents = _HERE.parents
    if len(parents) > 4:  # <hermes>/plugins/platforms/<plugin>/tests/_paths.py -> <hermes>/hermes-agent
        candidates.append(parents[4] / "hermes-agent")
    candidates += [pathlib.Path.cwd(), pathlib.Path.cwd() / "hermes-agent"]
    return next((c.resolve() for c in candidates if (c / "gateway").is_dir()), None)


RUNTIME = _find_runtime()

# ORDER MATTERS: executing the plugin package pulls in `gateway.*` (vk/__init__ -> adapter -> gateway),
# and gateway in turn imports top-level modules that live beside it (`hermes_yaml`). Put the runtime on
# sys.path FIRST — doing the registration first fails with "No module named 'hermes_yaml'" because
# `gateway` alone can resolve while its siblings cannot.
if RUNTIME is None:
    sys.stderr.write(MISSING_RUNTIME_HINT + "\n")
else:
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    _register_plugin_package()


def require_runtime() -> pathlib.Path:
    """The runtime directory, or a clear failure — used by the standalone (pytest-less) runners."""
    if RUNTIME is None:
        raise RuntimeError(MISSING_RUNTIME_HINT)
    return RUNTIME
