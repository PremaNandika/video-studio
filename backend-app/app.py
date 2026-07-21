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

  job_runner is a JobRunner class (state: cwd, job_env factory).
  The app factory constructs ONE instance and stashes it on
  app.config["JOB_RUNNER"].

  - Route handlers: current_app.config["JOB_RUNNER"]
  - Worker threads: receive the runner via cost_ctx (Rule 8.5)
  - Tests: construct directly with paths to a temp dir

DIRECTORY NAMING:
  Routes live in backend-app/routes/ (NOT blueprints/) — the app
  is API-only and "routes" is the clearer term.

REGISTRATION ORDER (matters!):
  1. register_auth() — installs the before_request PIN gate, which
     must be in place before any route can be protected.
  2. register_jobs() — the route module whose routes need the gate.
  3. register_library() — the /api/overview route.
  4. register_exports() — 5 output-manipulation routes (B5).
  5. register_scripts() — 2 script get/save routes (B6).
  6. register_dubbing() — the dub action of /api/run (B7).
  7. register_subtitles() — the clean-preview / clean-subs /
     clean-restore routes (B9). Stashes FFMPEG_BIN + ERASE_PY
     on app.config. Must register BEFORE captions() because
     the captions recaption route uses the same engines.
  8. register_captions() — caption + recaption + lines routes (B8).
  9. register_clone() — 5 /api/clone/* routes (B10). Stashes CLAUDE_EXE.
     Must register AFTER dubbing (CV_VENV_PY) + subtitles (FFMPEG_BIN).
  10. register_api_errors() — installs the app-level error handlers;
     MUST be called AFTER all route modules so it's the outermost
     layer (handlers don't get shadowed by route-level errors).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Flask

from routes.auth import register_auth
from routes.captions import register_captions
from routes.clone import register_clone
from routes.dubbing import register_dubbing
from routes.exports import register_exports
from routes.jobs import register_jobs
from routes.library import register_library
from routes.scripts import register_scripts
from routes.subtitles import register_subtitles
from services.api_errors import register_api_errors
from services.job_runner import JobRunner
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
    3. Compute derived paths (UPLOADS, TRANSCRIPTS, DESKTOP_VSLS,
       READY_DIR, SUBSTUDIO_OUT) and stash on app.config.
    4. register_auth() — installs the PIN gate before_request.
    5. register_jobs() — registers the 4 jobs routes.
    6. register_library() — registers the /api/overview route.
    7. register_exports() — registers the 5 output routes.
    8. register_scripts() — registers the 2 script get/save routes.
    9. register_dubbing() — registers the dub action of /api/run (B7).
    10. register_subtitles() — registers the 3 clean-* routes (B9).
    11. register_captions() — registers 5 caption routes (B8).
    12. register_clone() — registers 5 /api/clone/* routes (B10).
    13. register_api_errors() — JSON error handlers, outermost layer.
    """
    app = Flask(__name__)

    # Bind address.
    app.config["HOST"] = os.environ.get("HOST", "127.0.0.1")
    app.config["PORT"] = int(os.environ.get("PORT", "5181"))

    # Load config.json into app.config (Rule 8.1: only source of paths).
    cfg = _load_config_json()
    # Every path stored on app.config is a Path, never a raw string.
    # This lets helpers and route handlers do `cfg["FOO"] / "subdir"`
    # without per-call conversion. The JSON loader returns strings.
    app.config["AUTOVSL_ROOT"] = Path(cfg["autovsl_root"])
    app.config["ENGINES_DIR"] = Path(cfg["engines_dir"])
    app.config["EXPORTS_DIR"] = Path(cfg["exports_dir"])
    # ... other config.json keys would go here as the app grows.

    # Construct the spend service (Rule 17: class for stateful services).
    # All three path constants come from app.config — no hardcoded paths.
    autovsl_root = app.config["AUTOVSL_ROOT"]
    ledger_file = autovsl_root / "output" / "ledger.json"
    app.config["SPEND_LEDGER"] = SpendLedger(
        spend_ledger_file=ledger_file,
        autovsl_root=autovsl_root,
    )

    # Construct the job runner (Rule 17: class for stateful services).
    # The JobRunner holds the cwd (the autoVSL repo root, where
    # every engine subprocess is launched from) and the job_env
    # factory. B7a moved this from server.py's module-level
    # ``run_job`` function. Every route module that spawns an
    # engine subprocess will receive this instance via
    # ``current_app.config["JOB_RUNNER"]`` (or as a cost_ctx
    # arg in worker threads per Rule 8.5).
    def _default_job_env() -> dict:
        """Default factory for the subprocess env (B7a).

        Mirrors server.py's module-level ``job_env()``: prepends
        the Gyan ffmpeg bin dir to PATH and sets PYTHONUTF8=1.
        Centralized here so every engine subprocess sees the
        same env (faster-whisper needs ffmpeg on PATH;
        the UTF-8 flag protects non-ASCII filenames on Windows).
        """
        env = dict(os.environ)
        ffmpeg_bin = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Microsoft/WinGet/Packages"
            / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-8.1.2-full_build/bin"
        )
        if ffmpeg_bin.is_dir():
            env["PATH"] = str(ffmpeg_bin) + os.pathsep + env.get("PATH", "")
        env["PYTHONUTF8"] = "1"
        return env

    app.config["JOB_RUNNER"] = JobRunner(
        cwd=autovsl_root,
        job_env_factory=_default_job_env,
    )

    # Derived paths (B4 + B5): every constant server.py computes from
    # CONFIG/ROOT in lines 50-90 + 172-187. Computed once here so
    # route handlers and helpers read them via current_app.config
    # (Rule 8.1: paths come from app.config, not module globals).
    app.config["UPLOADS"] = autovsl_root / "uploads"
    app.config["TRANSCRIPTS"] = app.config["UPLOADS"] / "transcripts"
    app.config["DESKTOP_VSLS"] = Path.home() / "Desktop" / "litt VSL's"
    app.config["READY_DIR"] = Path(cfg["exports_dir"]) / "liitt testimonial Ready"
    # SUBSTUDIO_OUT is the recaption engine's output dir. The B5
    # commit set it to ``autovsl_root / "output" / "subtitle-studio"`` —
    # that's wrong. server.py L59 sets it to ``RECAPTION_PY.parent /
    # "output"``, which resolves to ``<subtitle-studio>/output``
    # (the recaption engine lives in a sibling subtitle-studio repo,
    # not inside autoVSL). Fixing this matches server.py exactly.
    # See .hermes/decisions/phase-2-2026-07-20-b8-captions.md.
    app.config["SUBSTUDIO_OUT"] = autovsl_root / ".." / "subtitle-studio" / "output"

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

    # Wire the library route module (B4: /api/overview).
    register_library(app)

    # Wire the exports route module (B5: 5 routes + DubWorkdir).
    register_exports(app)

    # Wire the scripts route module (B6: 2 routes, tight — script
    # get/save only; the rest of the Ads Factory cluster is B13).
    register_scripts(app)

    # Wire the dubbing route module (B7: POST /api/run/dub + dub_worker
    # thread target). The new app gets its own self-documenting URL
    # for the dub action — the legacy POST /api/run on server.py is
    # untouched (Rule 16) and still works for the old app.
    register_dubbing(app)

    # Wire the subtitles route module (B9: 3 clean-* routes +
    # clean_subs_worker thread target). Stashes FFMPEG_BIN + ERASE_PY
    # on app.config so the worker can call ffprobe and the engine
    # scripts can find the ProPainter-backed erase_subs.py. The
    # legacy POST /api/clean-* on server.py is untouched (Rule 16).
    register_subtitles(app)

    # Wire the captions route module (B8: 5 routes —
    # POST /api/run/caption, POST /api/run/recaption,
    # POST /api/recaption, GET/POST /api/captions/<stem>).
    register_captions(app)

    # Wire the clone route module (B10: 5 /api/clone/* routes —
    # winners, actors, script, run, list). Stashes CLAUDE_EXE on
    # app.config. Registered AFTER dubbing (CV_VENV_PY) and subtitles
    # (FFMPEG_BIN) so both keys are present. api_clone_run spawns the
    # dub via dub_worker (routes.dubbing) — finishes the B7c cutover.
    register_clone(app)

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
# Worker thread (dub_worker in routes/dubbing.py) receives runner + ledger
# via explicit args, NOT via app.config (per Rule 8.5 — no cross-tree
# imports in worker threads, pass dependencies explicitly):
#
#     from routes.dubbing import dub_worker
#
#     def start_dub(job_id, cmd, cost_ctx):
#         threading.Thread(
#             target=dub_worker,
#             args=(job_id, cmd, cost_ctx, runner, ledger),
#             daemon=True,
#         ).start()
