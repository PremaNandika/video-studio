"""JSON error responses for /api/* routes.

Triggered by the move of B3 jobs routes, which need 404 (no such
job) and 400 (wrong state) handlers that auth didn't already
cover. Per the B1 decision doc, the trigger to extract was
"the 2nd blueprint needs a 4xx handler that auth doesn't already
cover" — that fired here.

PATTERN:
  ``register_api_errors(app)`` is called once from
  ``create_app()`` after all blueprints are registered. It
  installs five ``app_errorhandler`` callbacks:

    - 400: JSON ``{"error": <msg>}`` for /api/*
    - 401: JSON for /api/* (used by the auth gate)
    - 403: JSON for /api/* (used by the auth gate + login route)
    - 404: JSON for /api/* (used by routes that look up a
      resource by id, e.g. /api/job/<id> when missing)
    - 500: JSON for /api/* (uncaught exception in a route —
      server.py defaults to HTML, we always return JSON)

  Non-/api/* paths fall through to Flask's default HTML error
  pages. Today there are no page routes in the new app (B2 was
  skipped — frontend is out of scope per the user), so the
  HTML fallback is dead code in practice, but it preserves the
  principle "JSON for APIs, HTML for pages" for when a future
  SvelteKit frontend is wired in.

  Note: 404 covers the "looked up a resource and it doesn't
  exist" case. The "URL itself doesn't match any route" case
  also returns 404 — and the 404 handler fires for that too,
  which is the correct behavior for /api/* paths.

  The 500 handler is the last-resort safety net. The original
  server.py returns HTML on uncaught exceptions; we return JSON.
  This is the only way a client can distinguish "your request
  broke the server" from "the server gave you HTML by accident"
  (e.g. when the autovsl_root config string isn't a Path).

WHY A FUNCTION, NOT A CLASS:
  Stateless. Rule 17 says class only when state persists across
  calls, or there are variants. Three handlers + one helper
  doesn't qualify. The ``register_*()`` pattern matches
  ``register_auth()`` and the future per-blueprint registers.
"""
from __future__ import annotations

from flask import Flask, jsonify, request


def _json_error(status: int, message: str):
    """Build a JSON error response: ``{"error": <message>}``."""
    resp = jsonify({"error": message})
    resp.status_code = status
    return resp


def _is_api_path() -> bool:
    """True if the current request path is under /api/."""
    return request.path.startswith("/api/")


def register_api_errors(app: Flask) -> None:
    """Install JSON error handlers on ``app`` for /api/* paths.

    Must be called after all blueprints are registered (so the
    handlers are the outermost layer) and before the first
    request is served.
    """

    @app.errorhandler(400)
    def _api_400(err):
        if _is_api_path():
            return _json_error(400, getattr(err, "description", "bad request"))
        return err  # default HTML for page routes (none today, future B2/Phase 3)

    @app.errorhandler(401)
    def _api_401(err):
        if _is_api_path():
            return _json_error(401, getattr(err, "description", "unauthorized"))
        return err

    @app.errorhandler(403)
    def _api_403(err):
        if _is_api_path():
            return _json_error(403, getattr(err, "description", "forbidden"))
        return err

    @app.errorhandler(404)
    def _api_404(err):
        if _is_api_path():
            return _json_error(404, getattr(err, "description", "not found"))
        return err

    @app.errorhandler(500)
    def _api_500(err):
        if _is_api_path():
            # Don't leak the original exception message to clients
            # (it can contain file paths, SQL, etc). Log it server-side
            # via Flask's default handler; return a generic message.
            return _json_error(500, "internal server error")
        return err
