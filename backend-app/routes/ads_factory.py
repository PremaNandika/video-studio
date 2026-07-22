"""Ads Factory route module — the /api/run residual (B13 S3).

Splits the leftover actions of server.py's ``POST /api/run`` dispatcher
(L429-594) into self-documenting per-action URLs, matching the B7/B8
pattern (dub/caption/recaption already split). After this slice the old
``/api/run`` is fully covered and retires at cutover.

  1. ``POST /api/run/transcribe``     (L440-456) — whisper transcription.
  2. ``POST /api/run/check-media``    — scripts/check-media.sh.
  3. ``POST /api/run/assemble``       — scripts/assemble-vsl.sh (+--no-music).
  4. ``POST /api/run/generate-vo``    — scripts/generate-vo.sh.
  5. ``POST /api/run/generate-video`` — scripts/generate-video.sh ($; confirm_cost).
  6. ``POST /api/run/print-prompts``  — scripts/print-prompts.sh.
  7. ``POST /api/run/list-models``    — scripts/generate-video.sh --list-models.

All shell scripts live in the ``autoVSL/scripts`` sibling tree (read-only,
Rule 8.5) and are launched via ``BASH`` (Git Bash) as subprocesses. No
worker threads here — each route bakes its ``cmd`` in request context and
spawns ``runner.run`` (so no current_app-in-thread concern).

PRESERVED BEHAVIOR:
  - ``transcribe`` GPU lock is auto-detected (``transcribe.py`` is in
    GPU_MARKERS) — no explicit ``job["gpu"]``, same as the monolith.
  - ``generate-video`` keeps the ``confirm_cost`` money gate + ``--shot``
    / ``--model`` (validated against ``VIDEO_MODELS``).
  - ``check-media`` → ``job["status"]="issues"`` on rc1 (JobRunner does this).
  - The ``bad slug`` guard (alnum + - + _) applies to every shell action.

DEPENDENCIES:
  - services.jobs:      jobs (job records)
  - services.job_runner via app.config["JOB_RUNNER"] (the engine runner)
  - app.config:         AUTOVSL_ROOT, UPLOADS, BASH, TRANSCRIBE_PY,
                        WHISPER_VENV_PY

server.py is unchanged. The /api/run dispatcher stays at its original
lines until the cutover. Rule 16.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.jobs import jobs


# fal.ai video models + their per-clip cost (server.py L176-180). Used to
# validate generate-video's --model.
VIDEO_MODELS = {
    "seedance-480p": 0.05, "seedance-720p": 0.11, "seedance-1080p": 0.24,
    "wan-5b-720p": 0.15, "wan-480p": 0.20, "hailuo-768p": 0.27,
    "wan-580p": 0.30, "kling-turbo": 0.35, "wan-720p": 0.40,
}

# The residual shell actions (subset of server.py's ACTIONS, L159-165) —
# dub/caption/recaption already moved (B7/B8); transcribe is special (below).
_RUN_ACTIONS = {
    "check-media":    {"script": "scripts/check-media.sh",    "label": "Check media"},
    "assemble":       {"script": "scripts/assemble-vsl.sh",   "label": "Assemble VSL"},
    "generate-vo":    {"script": "scripts/generate-vo.sh",    "label": "Generate VO (free)"},
    "generate-video": {"script": "scripts/generate-video.sh", "label": "Generate video (fal.ai, costs $)"},
    "print-prompts":  {"script": "scripts/print-prompts.sh",  "label": "Print prompts"},
    "list-models":    {"script": "scripts/generate-video.sh", "label": "List video models"},
}


ads_factory_bp = Blueprint("ads_factory", __name__)


# ---------------------------------------------------------------- helpers

def _check_slug(slug: str) -> str:
    """The monolith's slug guard (server.py L437): a slug lands on a shell
    command line, so it must be plain alnum + - + _."""
    if slug and not slug.replace("-", "").replace("_", "").isalnum():
        abort(400, "bad slug")
    return slug


def _spawn(action: str, slug: str, label: str, cmd: list[str], gpu: bool | None = None):
    """Create the job record + spawn ``runner.run`` on a daemon thread.

    Mirrors the monolith's job-dict shape. ``gpu`` left as None means the
    runner auto-detects from GPU_MARKERS (transcribe.py), matching
    server.py which set no ``gpu`` key for these actions.
    """
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "action": action, "slug": slug, "label": label,
           "status": "running", "lines": [], "returncode": None,
           "started": time.time(), "ended": None}
    if gpu is not None:
        job["gpu"] = gpu
    jobs[job_id] = job
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


def _spawn_shell(action_key: str, slug: str, tail: list[str] | None = None):
    """Build ``[BASH, <script>, slug|'fairy-flame', *tail]`` and spawn it.
    Mirrors server.py L565-594 for the generic shell actions."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    action = _RUN_ACTIONS[action_key]
    cmd = [current_app.config["BASH"], str(autovsl / action["script"]),
           slug or "fairy-flame"] + (tail or [])
    label = f"{action['label']} — {slug}" if slug else action["label"]
    return _spawn(action_key, slug, label, cmd)


# ---------------------------------------------------------------- routes

@ads_factory_bp.post("/api/run/transcribe")
def api_run_transcribe():
    """Local whisper transcription of an upload. Pure relocation of the
    ``action=="transcribe"`` branch (server.py L440-456)."""
    body = request.get_json(force=True) or {}
    uploads = current_app.config["UPLOADS"]
    fname = Path(body.get("file", "")).name
    src = uploads / fname
    if not fname or not src.is_file():
        abort(400, "file not found in uploads/")
    venv_py = Path(current_app.config["WHISPER_VENV_PY"])
    if not venv_py.is_file():
        abort(500, f"transcribe venv missing: {venv_py}")
    cmd = [str(venv_py), str(current_app.config["TRANSCRIBE_PY"]), str(src), "--out", str(uploads)]
    # GPU auto-detected (transcribe.py is a GPU_MARKER) — no explicit flag,
    # same as the monolith.
    return _spawn("transcribe", fname, f"Transcribe — {fname}", cmd)


@ads_factory_bp.post("/api/run/check-media")
def api_run_check_media():
    """Preflight the media for a product. Pure relocation (shell action)."""
    slug = _check_slug((request.get_json(force=True) or {}).get("slug", ""))
    return _spawn_shell("check-media", slug)


@ads_factory_bp.post("/api/run/assemble")
def api_run_assemble():
    """Assemble the VSL. Pure relocation (shell action; +--no-music)."""
    body = request.get_json(force=True) or {}
    slug = _check_slug(body.get("slug", ""))
    tail = ["--no-music"] if body.get("no_music") else []
    return _spawn_shell("assemble", slug, tail)


@ads_factory_bp.post("/api/run/generate-vo")
def api_run_generate_vo():
    """Generate the free VO (edge-tts). Pure relocation (shell action)."""
    slug = _check_slug((request.get_json(force=True) or {}).get("slug", ""))
    return _spawn_shell("generate-vo", slug)


@ads_factory_bp.post("/api/run/generate-video")
def api_run_generate_video():
    """Generate video via fal.ai — spends money. Pure relocation of the
    generate-video branch (server.py L572-584): confirm_cost gate +
    optional --shot / --model (validated against VIDEO_MODELS)."""
    body = request.get_json(force=True) or {}
    slug = _check_slug(body.get("slug", ""))
    if not body.get("confirm_cost"):
        abort(400, "generate-video requires confirm_cost:true (this action spends real money)")
    tail: list[str] = []
    shot = body.get("shot")
    if shot is not None:
        if not str(shot).isdigit():
            abort(400, "bad shot")
        tail += ["--shot", str(shot)]
    model = body.get("video_model")
    if model:
        if model not in VIDEO_MODELS:
            abort(400, f"unknown video model: {model}")
        tail += ["--model", model]
    return _spawn_shell("generate-video", slug, tail)


@ads_factory_bp.post("/api/run/print-prompts")
def api_run_print_prompts():
    """Print the generation prompts (debug). Pure relocation (shell action)."""
    slug = _check_slug((request.get_json(force=True) or {}).get("slug", ""))
    return _spawn_shell("print-prompts", slug)


@ads_factory_bp.post("/api/run/list-models")
def api_run_list_models():
    """List the fal.ai video models. Pure relocation of the list-models
    branch (server.py L566-567): the script runs with --list-models and
    no slug arg."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    action = _RUN_ACTIONS["list-models"]
    cmd = [current_app.config["BASH"], str(autovsl / action["script"]), "--list-models"]
    return _spawn("list-models", "", action["label"], cmd)


# ---------------------------------------------------------------- wire-up

def register_ads_factory(app) -> None:
    """Register the run Blueprint and stash ``TRANSCRIBE_PY``.

    ``TRANSCRIBE_PY`` mirrors server.py's ``COURSE_PIPELINE /
    "transcribe.py"`` (the whisper transcription script), derived from
    AUTOVSL_ROOT. ``BASH`` comes from app.py (config.json); the whisper
    venv (``WHISPER_VENV_PY``) is already stashed by register_captions.
    """
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["TRANSCRIBE_PY"] = str(autovsl / ".." / "course_pipeline" / "transcribe.py")
    app.register_blueprint(ads_factory_bp)
