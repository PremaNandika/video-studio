"""parse_body — the single request-body entry point for blueprints.

Every route that reads a JSON body calls ``parse_body(SomeModel)`` instead
of ``request.get_json(...).get(...)``. The helper is deliberately faithful
to the pre-pydantic route code so the move changes NO behavior:

  * It uses ``request.get_json(force=True) or {}`` — the exact call the
    monolith used. Malformed JSON and empty bodies therefore raise the same
    Werkzeug 400 as before, rendered as {"error": <werkzeug msg>} by the
    existing _api_400 handler.

  * A non-dict JSON top-level body (e.g. ``[1, 2]`` or ``"hi"``) is NOT
    "improved" into a clean 400. The old route called ``body.get(...)`` on
    it, which raised AttributeError -> HTTP 500 -> {"error": "internal
    server error"}. We reproduce that exactly (Rule 5: move, don't improve;
    the frontend never posts a non-dict body).

  * A pydantic ValidationError is funneled through ``abort(400, msg)`` so it
    lands in the app's EXISTING error format ({"error": <msg>}), not
    pydantic's / flask-pydantic's own envelope.

WHY A FUNCTION, NOT A CLASS (Rule 17): stateless helper, called everywhere.
"""

from __future__ import annotations

from typing import TypeVar

from flask import abort, request
from pydantic import BaseModel, ValidationError

M = TypeVar("M", bound=BaseModel)


def _first_error_message(exc: ValidationError) -> str:
    """Render the first pydantic error as ``"<field>: <msg>"``.

    Only reached when a model actually rejects an input — which the
    faithful-lenient pilot models never do. It exists so that stricter
    models added in a later, deliberate commit get a readable 400 body in
    the app's existing {"error": <msg>} shape.
    """
    errors = exc.errors()
    if not errors:
        return "invalid request body"
    err = errors[0]
    loc = ".".join(str(p) for p in err.get("loc", ())) or "body"
    return f"{loc}: {err.get('msg', 'invalid')}"


def parse_body(model_cls: type[M]) -> M:
    """Validate the JSON request body into ``model_cls``.

    Faithful to the pre-pydantic route: same get_json call, same 400 on
    malformed/empty bodies, same 500 on a non-dict body, and validation
    errors routed into the existing {"error": <msg>} format via abort(400).
    """
    body = request.get_json(force=True) or {}
    if not isinstance(body, dict):
        # Reproduce the monolith's AttributeError -> 500 for a non-dict body.
        # Attribute access on a non-dict raises exactly as ``body.get(...)``
        # did in the old route; the message matches, and _api_500 renders it.
        body.get  # noqa: B018  (intentional: raises AttributeError like the old code)
    try:
        return model_cls.model_validate(body)
    except ValidationError as exc:
        abort(400, _first_error_message(exc))
