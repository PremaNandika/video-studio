"""Flask app factory — Phase 1 + Phase 2 scaffold.

Single boot route returns 'Server Online.' as plain text. This is
the boot checkpoint before any route module is registered.

PATTERN (per BACKEND-REFACTOR-RULES.md Rule 17):
  spend is a SpendLedger class (state: fal_spend.json, in-memory
  data, cloned_stems cache, threading lock). The app factory
  constructs ONE instance and stashes it on app.config["SPEND_LEDGER"].

  - Route handlers: current_app.config["SPEND_LEDGER"]
  - Worker threads: receive the ledger via cost_ctx (Rule 8.5)
  - Tests: construct directly with paths to a temp dir

DIRECTORY NAMING:
  Routes live in backend-app/routes/ (NOT blueprints/) — the app
  is API-only and "routes" is the clearer term.

REGISTRATION ORDER (matters!):
  1. register_auth() — installs the before_request PIN gate, which
     must be in place before any route can be protected.
  2. register_jobs() — the route module whose routes need the gate.
  3. register_api_errors() — installs the app-level error handlers;
     MUST be called AFTER all route modules so it's the outermost
     layer (handlers don't get shadowed by route-level errors).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask

from routes.auth import register_auth
from routes.jobs import register_jobs
from services.api_errors import register_api_errors
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

    Order matters (see module docstring):
    1. Load config.json → app.config.
    2. Construct SpendLedger and stash on app.config["SPEND_LEDGER"].
    3. register_auth() — installs the PIN gate before_request.
    4. register_jobs() — registers the 4 jobs routes.
    5. register_api_errors() — JSON error handlers, outermost layer.
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

    # Wire the auth subsystem (B1: PIN gate + login/logout/ping).
    # Register BEFORE the index route so the before_request guard
    # fires for every request, including ones the gate rejects.
    register_auth(
        app,
        remote_pin=str(cfg.get("remote_pin", "") or ""),
        secret_key=str(cfg.get("secret_key", "dev-only-change-me")),
    )

    # Wire the jobs route module (B3: list/get/stop/resume).
    register_jobs(app)

    # Wire JSON error handlers (extracted from auth.py in B2.5).
    # Must be called AFTER all route modules are registered so the
    # handlers are the outermost layer.
    register_api_errors(app)

    @app.get("/")
    def index() -> tuple[str, int]:
        return "Server Online.\n", 200

    return app


# Example route that uses the SpendLedger from app.config:
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
