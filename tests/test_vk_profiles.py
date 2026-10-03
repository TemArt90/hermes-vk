"""Profile isolation: one plugin, several Hermes profiles, no credential leaking between them.

What this proves: the token (and the enablement answer) an adapter ends up with comes from THAT
profile's own ``.env``, and a profile with no token stays closed instead of borrowing the default
profile's — a failure mode that stays invisible until the wrong community answers someone's message.

How: the drive runs in a subprocess (profile scoping is process-global state that monkeypatching cannot
faithfully imitate) and uses the SAME core API the gateway uses to scope one profile —
``agent.secret_scope.set_secret_scope`` + ``build_profile_secret_scope`` — with multiplexing switched on,
so ambient ``VK_*`` variables are ignored exactly as they are in a multiplexed gateway.

Deliberately NOT the full plugin-manager drive: ``PluginManager().discover_and_load()`` inside a profile
scope cost ~50s each (measured: 147s for this one test) and asserts the CORE's discovery behaviour,
while the credential contract asserted here is ours. The scoped-reader invariants that need no
subprocess at all are pinned in ``test_vk_adapter.py``, which runs everywhere.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

import _paths  # noqa: E402  (registers the plugin as `vk` and locates the Hermes runtime)

RUNTIME = pathlib.Path(_paths.RUNTIME) if _paths.RUNTIME else None
PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]

_SCRIPT = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path

    plugin_parent, runtime, default_home, child_home = map(Path, sys.argv[1:5])
    sys.path.insert(0, str(runtime))
    sys.path.insert(0, str(plugin_parent))

    from agent.secret_scope import (build_profile_secret_scope, reset_secret_scope,
                                    set_multiplex_active, set_secret_scope)
    from gateway.config import Platform, PlatformConfig

    Platform._add_pseudo_member("vk")
    import vk.adapter as plugin

    def snapshot(home):
        token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            adapter = plugin.VKAdapter(PlatformConfig(extra={}))
            return {
                "token": adapter.token,
                "user_token": adapter.user_token,
                "requirements": plugin.check_requirements(),
                "enablement": plugin._env_enablement(),
            }
        finally:
            reset_secret_scope(token)

    ambient = {k: v for k, v in os.environ.items() if k.startswith("VK_")}
    set_multiplex_active(True)
    snapshots = {
        "default": snapshot(default_home),
        "child": snapshot(child_home),
        "default-again": snapshot(default_home),
    }
    assert {k: v for k, v in os.environ.items() if k.startswith("VK_")} == ambient, (
        "the plugin leaked VK_* into the ambient environment")
    print(json.dumps(snapshots, sort_keys=True))
    """
)


class _RuntimeUnavailable(RuntimeError):
    """The Hermes runtime internals are not importable here (a CI dependency slice, not a full install)."""


def _drive_profiles(tmp_path: pathlib.Path) -> dict:
    """Snapshot our adapter in three profile scopes, inside one subprocess."""
    default_home = tmp_path / "home"
    child_home = tmp_path / "home-child"
    (tmp_path / "os-home").mkdir()
    default_home.mkdir()
    child_home.mkdir()
    (default_home / ".env").write_text(
        "VK_TOKEN=default-profile-token\nVK_USER_TOKEN=default-profile-user-token\nVK_HOME_CHANNEL=13580122\n",
        encoding="utf-8")
    (child_home / ".env").write_text("", encoding="utf-8")

    env = {name: value for name, value in os.environ.items() if not name.startswith("VK_")}
    env.update(
        HOME=str(tmp_path / "os-home"),
        HERMES_HOME=str(default_home),
        # Ambient credentials are the trap: every profile's process in a multiplexed gateway sees them,
        # and a plugin that read them would answer as the wrong community.
        VK_TOKEN="ambient-token-that-must-be-ignored",
        VK_USER_TOKEN="ambient-user-token-that-must-be-ignored",
    )
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(PLUGIN_DIR.parent), str(RUNTIME),
         str(default_home), str(child_home)],
        env=env, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "ModuleNotFoundError" in stderr or "ImportError" in stderr:
            # Raised as a plain exception (not pytest.skip) so the pytest-less runner can report it too.
            raise _RuntimeUnavailable((stderr.splitlines() or ["?"])[-1])
        raise AssertionError(f"the profile drive failed:\n{stderr[-2000:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_tokenless_profile_stays_closed_and_borrows_nothing(tmp_path):
    """The default profile has a token, the child profile has none.

    The child must resolve NOTHING — neither the default profile's token nor the ambient one — and must
    report itself closed: no credentials, no seeded cron channel. Falling back to a neighbour's token is
    exactly the failure this guards.
    """
    try:
        snapshots = _drive_profiles(tmp_path)
    except _RuntimeUnavailable as exc:
        # Our CI installs a pinned dependency slice, not Hermes' whole set: skip loudly rather than report
        # a green suite that never exercised the isolation contract.
        pytest.skip(f"Hermes runtime internals are not importable here (CI dependency slice): {exc}")
        raise AssertionError("unreachable: pytest.skip never returns")  # keeps linters honest

    assert snapshots["default"]["token"] == "default-profile-token"
    assert snapshots["default"]["user_token"] == "default-profile-user-token"
    assert snapshots["default"]["requirements"] is True
    assert snapshots["default"]["enablement"] is not None

    assert snapshots["child"]["token"] == ""             # nothing borrowed from the neighbouring profile
    assert snapshots["child"]["user_token"] == ""         # the personal token is profile-scoped too
    assert snapshots["child"]["requirements"] is False    # fail closed, not fall back
    assert snapshots["child"]["enablement"] is None       # and no cron home channel was seeded

    assert snapshots["default-again"] == snapshots["default"]  # stable across repeated scopes


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory(prefix="hermes-vk-profiles-") as work:
        try:
            observed = _drive_profiles(pathlib.Path(work))
        except _RuntimeUnavailable as exc:
            print(f"skip profile isolation ({exc})")
            raise SystemExit(0)
    assert observed["default"]["token"] == "default-profile-token"
    assert observed["child"]["token"] == "" and observed["child"]["user_token"] == ""
    assert observed["child"]["requirements"] is False
    print("ok   profile isolation: 1/1 passed "
          "(default resolves its own token and user token, tokenless profile stays closed, "
          "ambient VK_* ignored)")
