from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name):
    # Deferred so `import app` stays cheap for tests that only want settings.
    if name == "create_app":
        from .main import create_app
        return create_app
    raise AttributeError(name)
