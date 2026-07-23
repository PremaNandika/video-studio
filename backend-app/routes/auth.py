"""Auth route module — PIN gate + login/logout/ping.

Pure relocation of video-studio/app/server.py L97-149. The gate
behavior, route behavior, and config are all byte-equivalent to
the original; only the wiring changed (before_request handler is
registered explicitly via ``register_auth()`` instead of being a
module-level ``@app.before_request`` decorator in server.py).

Routes owned by this module:
    POST /api/login       — sets the vs_auth session flag if the PIN matches
    POST /api/logout      — clears the session
    GET  /api/ping        — liveness probe (also bypasses the gate)

Note: the HTML ``GET /login`` page is frontend and is NOT owned by
this module. It will be served by a future frontend layer
(SvelteKit in Phase 3, or a transitional pages module). The
``/login`` path remains in ``_AUTH_OPEN`` so the gate never
blocks it pre-auth.

The ``before_request`` guard is registered separately via
``register_auth()`` so it fires for *every* request, including
ones that don't match any route.

LIFECYCLE:
    - ``register_auth(app, remote_pin, secret_key)`` is called once
      by the app factory in ``app.py::create_app()``.
    - The PIN and secret are stored on ``app.config``; the gate
      reads ``current_app.config["REMOTE_PIN"]`` at request time.

server.py is unchanged. The original L97-149 code stays at its
lines until the *entire* auth subsystem is retired. Rule 16.
"""
from __future__ import annotations

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    jsonify,
    redirect,
    request,
    session,
)

from schemas.auth_request import LoginRequest
from schemas.base import parse_body

# Same set the monolith uses. /login is included for defense in
# depth — the page itself is served by a future pages blueprint
# (B2), but the gate should never block it pre-auth either. The
# /static/... rule is the same: defense in depth.
_AUTH_OPEN = {"/login", "/api/login", "/favicon.ico", "/api/ping"}

# Path to the static dir that holds login.html. The blueprint
# itself doesn't know which dir the app uses — it gets it from
# the app factory via app.config["STATIC_DIR"]. Defaults to a
# sibling "static/" dir next to the package.
_SESSION_KEY = "vs_auth"


def _is_local(addr: str) -> bool:
    """True if the request came from this PC. Bypasses the PIN gate
    even when a PIN is configured (localhost is always trusted)."""
    return addr in ("127.0.0.1", "::1", "localhost") or (addr or "").startswith("127.")


def _require_pin() -> None:
    """The PIN gate. Registered via app.before_request in register_auth().

    Logic (byte-equivalent to server.py L111-123):
      - No PIN configured → no gate.
      - Local request → bypass.
      - Path in _AUTH_OPEN or /static/... → bypass.
      - Already authed (session flag) → bypass.
      - API path → 401 (don't redirect API callers).
      - Otherwise → redirect to /login?next=<original>.
    """
    pin = current_app.config.get("REMOTE_PIN", "")
    if not pin:                                # no PIN configured → no gate
        return
    if _is_local(request.remote_addr or ""):   # this PC → never prompt
        return
    if request.path in _AUTH_OPEN or request.path.startswith("/static/"):
        return
    if session.get(_SESSION_KEY):
        return
    if request.path.startswith("/api/"):
        abort(401)                             # API callers get 401, not a redirect
    return redirect("/login?next=" + request.path)


# The blueprint itself — routes are registered with the app via
# register_auth(). No url_prefix so paths stay exactly as in server.py.
auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/api/login")
def api_login():
    """Validates the PIN and sets the vs_auth session flag."""
    pin = current_app.config.get("REMOTE_PIN", "")
    supplied = parse_body(LoginRequest).pin
    if pin and str(supplied) == pin:
        session.permanent = True
        session[_SESSION_KEY] = True
        return jsonify({"ok": True})
    abort(403, "wrong PIN")


@auth_bp.post("/api/logout")
def api_logout():
    """Clears the session."""
    session.clear()
    return jsonify({"ok": True})


@auth_bp.get("/api/ping")
def api_ping():
    """Liveness probe. Always returns 200 with the app name."""
    return jsonify({"ok": True, "app": "video-studio"})


def register_auth(app: Flask, remote_pin: str, secret_key: str) -> None:
    """Wire the auth subsystem into ``app``.

    Three things happen, in order:

    1. ``app.secret_key`` is set (required for session signing).
    2. The PIN is stashed on ``app.config["REMOTE_PIN"]`` so the
       gate and the login route can read it via current_app.
    3. The blueprint is registered AND the before_request guard is
       attached at the app level (not the blueprint level, so it
       fires for *every* request).
    """
    app.secret_key = secret_key
    app.config["REMOTE_PIN"] = str(remote_pin or "")
    app.before_request(_require_pin)
    app.register_blueprint(auth_bp)
