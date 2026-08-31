"""The HTTP surface, one module per group of routes.

Routes are closures over the app they were registered on, the way they were
when they all lived in `app.main` — `register(app)` runs before the web
build's auth middleware is installed, so the gate wraps everything here.
The admin routes register separately: only the web build has users to manage.
"""
from __future__ import annotations

from fastapi import FastAPI

from . import (admin, canvas, compare, files, jobs, models, presets, promo,
               prompts, quest, sapling, settings, styles, version, watch)


def register(app: FastAPI) -> None:
    files.register(app)
    compare.register(app)
    version.register(app)
    models.register(app)
    presets.register(app)
    jobs.register(app)
    styles.register(app)
    prompts.register(app)
    watch.register(app)
    promo.register(app)
    quest.register(app)
    sapling.register(app)
    settings.register(app)
    canvas.register(app)


def register_admin(app: FastAPI) -> None:
    admin.register(app)
