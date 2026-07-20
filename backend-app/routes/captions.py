"""Captions route module — caption + recaption + edited-lines endpoints.

Pure move of four blocks from video-studio/app/server.py:

  1. The ``if action == "caption":`` branch of ``/api/run``
     (server.py L458-476, ~19 lines) — moved to ``caption_action()``,
     exposed as ``POST /api/run/caption``.
  2. The ``if action == "recaption":`` branch of ``/api/run``
     (server.py L478-495, ~18 lines) — moved to ``recaption_action()``,
     exposed as ``POST /api/run/recaption``.
  3. The ``POST /api/recaption`` standalone route
     (server.py L2276-2308, ~33 lines) — moved to
     ``api_recaption_action()`` and exposed at the same URL
     (``POST /api/recaption``). The 4 ``mode`` values
     (``captions`` | ``burn-lines`` | ``cover`` | ``no-captions``)
     are preserved.
  4. The ``GET/POST /api/captions/<stem>`` lines.json read/save
     routes (server.py L2311-2330, ~20 lines) — moved to
     ``captions_get`` and ``captions_save``, exposed at the
     same URLs.

Plus 1 module constant (``CAPTION_VIDEO_EXTS``) and
1 mode→flags map (the 4 modes inlined in server.py).

URL CHOICE — MATCHING B7
  The legacy server.py has three caption-related entry points:
    - ``POST /api/run`` with ``{"action": "caption", ...}``
    - ``POST /api/run`` with ``{"action": "recaption", ...}``
    - ``POST /api/recaption`` (standalone, with 4 modes)
  The new app exposes each as a self-documenting URL:
    - ``POST /api/run/caption``         (the ``action=="caption"`` branch)
    - ``POST /api/run/recaption``       (the ``action=="recaption"`` branch)
    - ``POST /api/recaption``           (unchanged URL, 4 modes)
    - ``GET/POST /api/captions/<stem>`` (unchanged URLs, read/save lines.json)

  Same pattern as B7: each blueprint owns its routes, no
  shared dispatchers. The legacy server.py keeps its
  ``POST /api/run`` and all three caption entry points.
  Both apps run side by side.

WHAT IS NOT HERE (deferred to future commits):
  - The ``transcribe`` branch of ``/api/run`` (server.py
    L440-456). It shares the WHISPER_VENV_PY config key
    but is a separate concern (no captions engine).
    Lands in a future "Transcribe" blueprint (TBD; not
    currently in the B8-B13 roadmap).
  - The ``POST /api/recaption-from-product`` and the
    ``POST /api/captions/<stem>/aifix`` routes
    (server.py L2341-2411ish). These wrap recaption +
    a spell-fix LLM call. Belong in a future "AIFix"
    blueprint (B12 chat scope or a B8.5 split).
  - The ``GET /captioned/<stem>`` route (server.py L2333)
    — a media-file server, B2-style page route (out of
    scope per the user's "frontend is someone else's" rule).
  - The QC tab's ``/api/qc/*`` consumer of lines.json.
    QC is its own blueprint (B-something).
  - The actual deletion of the caption/recaption branches
    from server.py (B8c cutover). Deferred until the
    transcribe branch also moves (so the dispatcher can
    be retired all at once).

DEPENDENCIES:
  - services.jobs:        jobs dict + jobs_lock (the in-memory store)
  - services.job_runner:  JobRunner (the shared engine subprocess runner)
  - services.workdir:     DubWorkdir (the script_edited + final paths
                          used by the caption actions)
  - services.spend:       read_json (for the lines.json loader)

LIFECYCLE (per Rule 8.1):
  - All paths come from ``current_app.config``: ``UPLOADS``,
    ``ENGINES_DIR``, ``WHISPER_VENV_PY``, ``RECAPTION_PY``,
    ``SUBSTUDIO_OUT``, ``AUTOVSL_ROOT``.
  - The worker thread receives the runner as an explicit arg
    (Rule 8.5: no cross-tree imports in worker threads, pass
    dependencies explicitly).
  - ``register_captions(app)`` is the wire-up point. It
    computes and stashes ``WHISPER_VENV_PY`` and
    ``RECAPTION_PY`` on ``app.config`` (it does NOT touch
    ``SUBSTUDIO_OUT`` — that derivation is owned by
    ``app.py``).

server.py is unchanged. The four caption/recaption
blocks stay at their original lines until the B8c
cutover lands. Rule 16.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.jobs import jobs, jobs_lock
from services.workdir import DubWorkdir


# ---------------------------------------------------------------- constants

CAPTION_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}

# Mode → CLI flag mapping for the POST /api/recaption standalone route.
# The first value (captions) is the empty flag list — it relies on
# the engine's default behaviour (transcribe + burn + cover).
_RECAPTION_MODES = {
    "captions":     [],
    "burn-lines":   ["--burn-lines"],
    "cover":        None,  # special: see api_recaption_action (needs --cover-style)
    "no-captions":  ["--no-captions"],
}


# ---------------------------------------------------------------- module helpers

def _read_json(path: Path):
    """Safe JSON loader — returns None on any error (missing/bad).

    Mirrors server.py's read_json() at L1613. We don't import
    the spend-service copy (Rule 5.1: services don't depend
    on each other) and don't promote this to a shared
    services/helpers/common.py helper until a 3rd call site
    needs it (per the "wait for 3rd use" rule in common.py's
    docstring).
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- HTTP routes

captions_bp = Blueprint("captions", __name__)


# -- 1. POST /api/run/caption (was /api/run action=="caption") ---------

@captions_bp.post("/api/run/caption")
def api_run_caption():
    """Burn captions over a dubbed video (the ``action=="caption"``
    branch of legacy /api/run).

    Body (JSON): ``{"file": "<stem>"}``. The ``file`` is just a
    stem (no path) — the action looks up
    ``output/script-swap/<stem>/final.mp4`` and
    ``output/script-swap/<stem>/new-vo.mp3`` (both must exist;
    they are produced by the dub pass).

    Response: ``{"job_id": "..."}`` on success; 4xx JSON on
    missing files or missing venv.

    Pure relocation of server.py L458-476.
    """
    body = request.get_json(force=True) or {}
    return caption_action(body)


def caption_action(body: dict):
    """The relocated ``if action == "caption":`` branch.

    Pure relocation of server.py L458-476. The body is
    byte-equivalent; the only deltas are:
      - ``UPLOADS`` / ``SWAP_WORK`` / ``TRANSCRIBE_VENV_PY``
        globals → ``current_app.config`` reads
      - ``DubWorkdir(autovsl, stem)`` replaces the inline
        ``SWAP_WORK / stem`` path construction
      - The worker thread receives the ``runner`` from
        ``app.config`` (Rule 8.5)
      - The job's ``action`` field is hardcoded to
        ``"caption"`` (the legacy code used the action
        dispatch var; here we're inside a caption-only
        handler)
    """
    stem = Path(body.get("file", "")).name
    autovsl = current_app.config["AUTOVSL_ROOT"]
    wd = DubWorkdir(autovsl, stem)
    if not stem or not wd.final.is_file():
        abort(400, "no dubbed final for that video — dub first")
    if not wd.new_vo.is_file():
        abort(400, "no VO in the work dir — re-run the dub")
    venv_py = Path(current_app.config["WHISPER_VENV_PY"])
    if not venv_py.is_file():
        abort(500, "transcribe venv missing (needed for word timing)")
    engines_dir = current_app.config["ENGINES_DIR"]
    cmd = [str(venv_py), str(engines_dir / "caption.py"), "--name", stem]
    return _spawn_job("caption", stem, f"Burn captions — {stem}", cmd, gpu=True)


# -- 2. POST /api/run/recaption (was /api/run action=="recaption") -----

@captions_bp.post("/api/run/recaption")
def api_run_recaption():
    """Re-caption from the original audio (the ``action=="recaption"``
    branch of legacy /api/run).

    Body (JSON): ``{"file": "<filename>.mp4"}``. Looks up the
    file in the uploads/ dir; runs the recaption engine against
    the original video (not the dubbed one) to produce fresh
    word-timed captions.

    Pure relocation of server.py L478-495.
    """
    body = request.get_json(force=True) or {}
    return recaption_action(body)


def recaption_action(body: dict):
    """The relocated ``if action == "recaption":`` branch.

    Pure relocation of server.py L478-495. Same deltas as
    ``caption_action`` (config reads, worker thread pattern,
    hardcoded action name).
    """
    fname = Path(body.get("file", "")).name
    uploads = current_app.config["UPLOADS"]
    src = uploads / fname
    if not fname or not src.is_file():
        abort(400, "file not found in uploads/")
    venv_py = Path(current_app.config["WHISPER_VENV_PY"])
    if not venv_py.is_file():
        abort(500, "transcribe venv missing (needed for word timing)")
    engines_dir = current_app.config["ENGINES_DIR"]
    cmd = [str(venv_py), str(engines_dir / "caption.py"),
           "--video", str(src)]
    return _spawn_job("recaption", src.stem,
                      f"New subtitles — {fname} (from original audio)",
                      cmd, gpu=True)


# -- 3. POST /api/recaption (the standalone 4-mode dispatcher) ---------

@captions_bp.post("/api/recaption")
def api_recaption():
    """Caption a video with the subtitle-studio recaption engine
    (4 modes: ``captions`` | ``burn-lines`` | ``cover`` | ``no-captions``).

    Body (JSON): ``{"path": "<repo-relative .mp4>", "mode": "<mode>",
    "style": "blur"|"box" (only for cover mode)}``. The path
    must resolve to a video file inside the data root (server.py
    uses ``ROOT / rel`` and checks the file extension; the
    check below mirrors that — same allow-list, same error).

    Pure relocation of server.py L2276-2308.
    """
    body = request.get_json(force=True)
    return api_recaption_action(body)


def api_recaption_action(body: dict):
    """The relocated ``api_recaption`` body.

    Pure relocation of server.py L2276-2308. Behavior:
      - Validates ``path`` (must be a video inside the data root)
      - Validates ``mode`` (one of the 4 in ``_RECAPTION_MODES``)
      - Validates the whisper venv is present (abort 500 if not)
      - Builds the engine command, spawns a worker thread
      - Sets ``job["gpu"] = True`` ONLY for ``captions`` mode
        (the other 3 modes are pure ffmpeg — no whisper
        transcribe pass, no GPU need)
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    rel = (body.get("path") or "").replace("\\", "/")
    src = (autovsl / rel).resolve()
    if not str(src).startswith(str(autovsl)) or src.suffix.lower() not in CAPTION_VIDEO_EXTS:
        abort(400, "path must be a video inside the data root")
    if not src.is_file():
        abort(404, "video not found")
    mode = body.get("mode", "captions")
    if mode == "cover":
        flags = ["--cover", "--cover-style", body.get("style", "blur")]
    else:
        flags = _RECAPTION_MODES.get(mode)
        if flags is None:
            abort(400, "bad mode")
    venv_py = Path(current_app.config["WHISPER_VENV_PY"])
    if not venv_py.is_file():
        abort(500, f"whisper venv missing: {venv_py}")
    recaption_py = Path(current_app.config["RECAPTION_PY"])
    cmd = [str(venv_py), str(recaption_py), str(src)] + flags
    # only full caption runs transcribe; re-burn/cover are pure ffmpeg
    gpu = mode == "captions"
    return _spawn_job("recaption", src.stem,
                      f"Captions ({mode}) — {src.name}", cmd, gpu=gpu)


# -- 4/5. GET/POST /api/captions/<stem> (read/save lines.json) --------

@captions_bp.get("/api/captions/<stem>")
def api_captions_get(stem):
    """Return the per-workdir edited lines.json + captioned.mp4
    state for the Captions tab's line-editor UI.

    Response: ``{"lines": [...], "captioned": bool,
    "captioned_mtime": float|null}``.

    Pure relocation of server.py L2311-2318. Looks up the
    workdir under ``SUBSTUDIO_OUT/<stem>/``.
    """
    stem = Path(stem).name
    substudio_out = current_app.config["SUBSTUDIO_OUT"]
    work = substudio_out / stem
    cap = work / "captioned.mp4"
    return jsonify({"lines": _read_json(work / "lines.json") or [],
                    "captioned": cap.is_file(),
                    "captioned_mtime": cap.stat().st_mtime if cap.is_file() else None})


@captions_bp.post("/api/captions/<stem>")
def api_captions_save(stem):
    """Save the per-workdir edited lines.json (the user-edited
    caption lines from the line-editor UI).

    Body (JSON): ``{"lines": [...]}``. Validates that ``lines``
    is a non-empty list, then writes the JSON file.

    Pure relocation of server.py L2321-2330.
    """
    stem = Path(stem).name
    lines = request.get_json(force=True).get("lines")
    if not isinstance(lines, list) or not lines:
        abort(400, "no lines")
    substudio_out = current_app.config["SUBSTUDIO_OUT"]
    work = substudio_out / stem
    work.mkdir(parents=True, exist_ok=True)
    (work / "lines.json").write_text(json.dumps(lines, indent=1), encoding="utf-8")
    return jsonify({"saved": len(lines)})


# ---------------------------------------------------------------- worker / spawn

def _spawn_job(action: str, slug: str, label: str, cmd: list[str],
               gpu: bool = False):
    """Allocate a job_id, build the job dict, spawn the
    JobRunner thread, return the JSON response.

    Shared by all three action functions (caption, recaption,
    api_recaption). Pure factory — the only state it touches
    is the global ``jobs`` dict (the in-memory store).
    """
    job_id = uuid.uuid4().hex[:8]
    job = {
        "id": job_id, "action": action, "slug": slug,
        "label": label, "status": "running",
        "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
    }
    if gpu:
        job["gpu"] = True
    jobs[job_id] = job
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(
        target=runner.run, args=(job_id, cmd), daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ---------------------------------------------------------------- wire-up

def register_captions(app) -> None:
    """Register the captions Blueprint on ``app`` and stash
    the whisper venv path + recaption engine path on
    ``app.config``.

    Two config keys are set:
      - ``WHISPER_VENV_PY`` — the faster-whisper venv's
        ``python.exe``, used by every caption action.
      - ``RECAPTION_PY`` — the recaption engine script.

    Both paths are derived from
    ``app.config["AUTOVSL_ROOT"]`` + the standard
    ``../course_pipeline/.venv/Scripts/python.exe`` and
    ``../subtitle-studio/recaption.py`` layout (mirrors
    ``CONFIG["venvs"]["whisper"]`` and
    ``CONFIG["engines"]["recaption"]``).

    NOTE: this function does NOT touch ``SUBSTUDIO_OUT``.
    The correct derivation for that key is the recaption
    engine's parent / "output" (matches ``server.py`` L59).
    That fix is applied in ``app.py``'s ``create_app`` —
    not in this Blueprint's wire-up — so the bug fix is
    its own atomic commit (see the B8 decision doc).
    """
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["WHISPER_VENV_PY"] = str(
        autovsl / ".." / "course_pipeline" / ".venv" / "Scripts" / "python.exe"
    )
    app.config["RECAPTION_PY"] = str(
        autovsl / ".." / "subtitle-studio" / "recaption.py"
    )
    app.register_blueprint(captions_bp)
