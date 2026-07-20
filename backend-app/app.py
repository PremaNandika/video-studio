"""Flask app factory — Phase 1 scaffold.

A single route returns 'Server Online.' as plain text. This is the boot
checkpoint before any blueprint is registered.

See docs/REFACTOR-PLAN.md (Phase 1) and docs/BACKEND-REFACTOR-RULES.md
for the source-of-truth hierarchy and the working discipline.
"""
from __future__ import annotations

import os

from flask import Flask


def create_app() -> Flask:
    """Build and return the Flask app.

    Order matters: configuration first, then logging defaults, then
    blueprint registration (added one at a time in subsequent commits —
    see Rule 5.2, one blueprint per commit).
    """
    app = Flask(__name__)

    # Single source of truth for the bind address.
    # The live video-studio app owns 5180; the new backend uses 5181 in
    # Phase 1 to avoid collision. Phase 3d cuts over to 5180.
    app.config["HOST"] = os.environ.get("HOST", "127.0.0.1")
    app.config["PORT"] = int(os.environ.get("PORT", "5181"))

    @app.get("/")
    def index() -> tuple[str, int]:
        return "Server Online.\n", 200

    return app
