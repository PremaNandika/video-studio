"""Flask app factory — Phase 1 scaffold with SpendLedger wired.

Single boot route returns 'Server Online.' as plain text. This is
the boot checkpoint before any blueprint is registered.

PATTERN (per BACKEND-REFACTOR-RULES.md Rule 17):
  spend is a SpendLedger class (state: fal_spend.json, in-memory
  data, cloned_stems cache, threading lock). The app factory
  constructs ONE instance and stashes it on app.config["SPEND_LEDGER"].

  - Route handlers: current_app.config["SPEND_LEDGER"]
  - Worker threads: receive the ledger via cost_ctx (Rule 8.5)
  - Tests: construct directly with paths to a temp dir
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask

from services.spend import SpendLedger


def _load_config_json() -> dict:
    """Load backend-app/config.json (gitignored in dev, present in prod).

    The new side has its own config (separate from the legacy
    video-studio/config.json that server.py reads, which is
    immutable per Rule 16). Per Rule 8.1: hard-fail on missing
    config (no fallback).
    """
    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(
            f"config.json not found at {config_path}. "
            "Create it from a template before running the app "
            "(see backend-app/config.json in this directory)."
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def create_app() -> Flask:
    """Build and return the Flask app.

    Order matters:
    1. Load config.json → app.config.
    2. Construct SpendLedger and stash on app.config["SPEND_LEDGER"].
    3. Register blueprints (added one at a time in subsequent commits).
    """
    app = Flask(__name__)

    # Bind address.
    app.config["HOST"] = os.environ.get("HOST", "127.0.0.1")
    app.config["PORT"] = int(os.environ.get("PORT", "5181"))

    # Load config.json into app.config (Rule 8.1: only source of paths).
    cfg = _load_config_json()
    app.config["AUTOVSL_ROOT"] = cfg["autovsl_root"]
    app.config["ENGINES_DIR"] = cfg["engines_dir"]
    app.config["EXPORTS_DIR"] = cfg["exports_dir"]
    # ... other config.json keys would go here as the app grows.

    # Construct the spend service (Rule 17: class for stateful services).
    # All three path constants come from app.config — no hardcoded paths.
    autovsl_root = Path(app.config["AUTOVSL_ROOT"])
    ledger_file = autovsl_root / "output" / "ledger.json"
    app.config["SPEND_LEDGER"] = SpendLedger(
        spend_ledger_file=ledger_file,
        autovsl_root=autovsl_root,
    )

    @app.get("/")
    def index() -> tuple[str, int]:
        return "Server Online.\n", 200

    return app


# Example blueprint that uses the SpendLedger from app.config:
#
#     from flask import Blueprint, current_app, jsonify
#
#     bp = Blueprint("library", __name__)
#
#     @bp.get("/api/overview")
#     def overview():
#         ledger = current_app.config["SPEND_LEDGER"]
#         return jsonify({
#             "fal_spend": round(float(ledger.load().get("total", 0.0)), 2)
#         })
#
# Worker thread (e.g. run_dub_job) receives the ledger via cost_ctx:
#
#     def run_dub_job(job_id, cmd, cost_ctx):
#         ledger: SpendLedger = cost_ctx["spend_ledger"]
#         info = ledger.estimate_dub_cost(...)
#         ledger.record(...)
