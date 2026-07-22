"""Spend route module — the fal.ai spend dashboard read (B13 S1).

Pure move of one route from video-studio/app/server.py:

  ``GET /api/spend`` (server.py L422-426) — the running fal.ai total +
  the last 20 runs, for the spend widget in the UI.

Smallest B13 slice: a single read route over the existing
``SpendLedger`` service (S3). No new service, no new config key, no
helper module, no worker.

The legacy ``load_spend()`` module function becomes
``current_app.config["SPEND_LEDGER"].load()`` — the same dict shape
(``{total, runs, cloned_stems}``), so the response is byte-equivalent.

server.py is unchanged. The ``/api/spend`` block stays at its original
line until the cutover. Rule 16.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify


spend_bp = Blueprint("spend", __name__)


@spend_bp.get("/api/spend")
def api_spend():
    """Running fal.ai spend total + the last 20 runs.

    Pure relocation of server.py L422-426. Reads the ledger via the
    ``SpendLedger`` instance on ``app.config`` instead of the module
    -level ``load_spend()``; the returned shape is identical.
    """
    d = current_app.config["SPEND_LEDGER"].load()
    return jsonify({"total": round(float(d.get("total", 0.0)), 2),
                    "runs": list(reversed(d.get("runs", [])))[:20]})


def register_spend(app) -> None:
    """Register the spend Blueprint. No config keys of its own — the
    ``SPEND_LEDGER`` it reads is constructed in ``app.py`` (S3)."""
    app.register_blueprint(spend_bp)
