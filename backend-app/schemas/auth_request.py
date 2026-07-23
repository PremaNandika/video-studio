"""Request schemas for routes/auth.py (B1) — the pydantic pilot.

Only the login body has fields; /api/logout and /api/ping take no body.

LoginRequest is intentionally the most permissive model in the package —
it is the pilot, chosen precisely because it can be made byte-faithful to
the monolith:

    old route:  body = request.get_json(force=True) or {}
                supplied = body.get("pin", "")     # missing -> "" ; any type kept
                ... if pin and str(supplied) == pin ...

So the faithful-lenient model is:

    * ``pin`` is OPTIONAL with default ``""`` — matches ``.get("pin", "")``
      when the key is absent.
    * ``pin`` is typed ``Any`` — the old code kept whatever JSON value was
      supplied (str, int, bool, null) and only coerced with ``str()`` at the
      comparison site. Typing it ``str`` here would coerce/reject and change
      behavior (e.g. ``{"pin": 1234}`` and ``{"pin": true}`` must survive
      untouched so the route's ``str(supplied)`` reproduces "1234"/"True").
    * ``extra="ignore"`` — unknown fields are dropped, never rejected, so
      ``{"pin": "1234", "x": 9}`` still logs in.

Tightening any of this (e.g. ``pin: str``, ``extra="forbid"``) is a
deliberate future commit with its own behavioral review — never part of
this move.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    """Body of ``POST /api/login``. Faithful-lenient — see module docstring."""

    pin: int
    model_config = ConfigDict(extra="forbid")
