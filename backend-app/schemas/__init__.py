"""Request/response schemas for the backend-app blueprints.

Introduced in the Phase-1 pydantic pass. Structure:

    schemas/
      base.py    — parse_body(): the one entry point every route uses to
                   turn a JSON request body into a validated pydantic model.
                   Validation failures are funneled through the app's
                   EXISTING error path (abort(400) -> services/api_errors.py
                   -> {"error": <msg>}), so no new error shape is introduced.
      auth_request.py       — LoginRequest (piloted first; smallest blueprint).
      <routeName>_request.py — one module per blueprint as the rollout
                   proceeds. Naming standard: request-schema modules are
                   named after their route module with a ``_request`` suffix
                   (routes/auth.py -> schemas/auth_request.py).

DESIGN RULES (why this looks the way it does):

  1. Request-only for now. Response models are a later, separate concern —
     the routes already return hand-built dicts and changing that is a
     behavior risk we are not taking in the pilot.

  2. Faithful-lenient. A model must never REJECT an input the monolith
     accepted. Unknown fields are ignored (extra="ignore"); fields the old
     code read with a default are Optional with that same default; types are
     kept permissive where the old code coerced at use-site (e.g. str(x)).
     Tightening validation is a deliberate future commit, never a side
     effect of the move.

  3. Errors funnel into the existing format. parse_body() raises via Flask's
     abort(400, msg); the already-registered _api_400 handler renders it as
     {"error": msg}. flask-pydantic (which has its own error envelope) is
     intentionally NOT used.

  4. No behavior change. Each blueprint's wiring is verified with a
     before/after behavioral diff across a battery of inputs (including the
     pathological ones the monolith mishandled) before it is committed.
"""
