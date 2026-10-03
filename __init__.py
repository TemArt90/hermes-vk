from __future__ import annotations

if __package__:
    # The real path: Hermes' plugin loader imports this directory as a package
    # (`hermes_plugins.platforms__vk`), so the relative import resolves. Any failure here must
    # propagate — a plugin entry point that silently degrades to None is worse than a loud error.
    from .adapter import register
else:
    # A bare import of this file as a top-level module — pytest does that when resolving the package
    # of a checkout whose directory name is not a valid identifier (a plain `git clone` of
    # `hermes-vk`, for instance), where a relative import cannot work. Nothing on that path needs
    # `register`, so stay importable instead of breaking test collection.
    register = None

__all__ = ["register"]
