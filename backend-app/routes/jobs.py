"""Jobs route module — list/get/stop/resume for in-flight and historical jobs.

Pure relocation of video-studio/app/server.py L604-625 + L628-662
(the 4 job-related routes + ``job_progress`` helper + ``DUB_STAGE_LABEL``
constant).

Routes owned by this module:
    GET  /api/jobs                 — list all jobs (no log lines, with progress)
    GET  /api/job/<job_id>         — get one job + paginated log lines
    POST /api/job/<job_id>/stop    — kill a running job's process tree
    POST /api/job/<job_id>/resume  — re-run an interrupted/failed/stopped job

All routes return JSON. Errors (404 missing, 400 wrong state, 400
predates resume, 400 cloud-dub blind-resume) are converted to JSON
by ``services/api_errors.py`` (registered in create_app()).

DEPENDENCIES:
  - services.jobs: ``jobs`` (the in-memory dict), ``jobs_lock`` (the
    threading lock), ``set()`` for the keep-awake handle (returned
    by job runner, not used here). No job *creation* in this module
    — that's the dub/caption/erase/recaption routes' job.
  - subprocess: only the ``taskkill`` Windows call for /stop.
  - re: only for ``job_progress`` (parses tqdm + stage lines).
  - time: only for /stop (sets job["ended"] etc).

server.py is unchanged. The 4 routes, ``job_progress``, and
``DUB_STAGE_LABEL`` stay at their original lines until the entire
jobs subsystem is retired. Rule 16.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid

from flask import Blueprint, abort, current_app, jsonify, request

# Pure relocation of the 3 job-store symbols from server.py L154-155.
# The "from jobs import ..." line in server.py becomes this import
# after the service move (S1 already relocated services/jobs.py).
from services.jobs import jobs, jobs_lock


# Same set the monolith uses at L597-601 — maps engine stage names
# (emitted as "=== stage: <name>" in the engine's stdout) to
# human-friendly labels for the UI progress bar.
DUB_STAGE_LABEL = {
    "local-voice": "voice clone (XTTS)", "clone": "voice clone", "speak": "speech",
    "lipsync": "lip-sync", "hd": "HD face restore (GFPGAN)", "mux": "finishing",
    "captions": "captions",
}


def job_progress(job: dict) -> dict | None:
    """Parse a live percentage + stage from a running job's output
    (tqdm + stage lines). Returns None for non-running jobs.

    Pure relocation of server.py L604-625.
    """
    if job["status"] != "running":
        return None
    stage = pct = frac = None
    for line in job["lines"]:
        m = re.search(r"=== stage:\s*([\w-]+)", line)
        if m:
            stage = m.group(1)
    for line in reversed(job["lines"][-40:]):          # newest tqdm reading
        m = re.search(r"(\d+)%\|", line)
        if m:
            pct = int(m.group(1))
            break
        m2 = re.search(r"\b(\d+)/(\d+)\b", line)
        if m2 and frac is None:
            a, b = int(m2.group(1)), int(m2.group(2))
            if b:
                frac = round(100 * a / b)
    p = pct if pct is not None else frac
    label = DUB_STAGE_LABEL.get(stage, stage or "working")
    return {"pct": p, "stage": stage, "label": label}


jobs_bp = Blueprint("jobs", __name__)


@jobs_bp.get("/api/jobs")
def api_jobs():
    """List all jobs, newest first. No log lines (use GET /api/job/<id>)."""
    with jobs_lock:
        out = [
            {k: v for k, v in j.items() if k != "lines"}
            | {"line_count": len(j["lines"]), "progress": job_progress(j)}
            for j in sorted(jobs.values(), key=lambda j: j["started"], reverse=True)
        ]
    return jsonify(out)


@jobs_bp.get("/api/job/<job_id>")
def api_job(job_id):
    """Get one job + paginated log lines (use ?offset=N to continue)."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    offset = int(request.args.get("offset", 0))
    with jobs_lock:
        lines = job["lines"][offset:]
        return jsonify({
            "id": job["id"], "label": job["label"], "status": job["status"],
            "returncode": job["returncode"], "lines": lines,
            "next_offset": offset + len(lines),
            "cost": job.get("cost"),
        })


@jobs_bp.post("/api/job/<job_id>/stop")
def api_job_stop(job_id):
    """User-controlled cancel: kill the job's whole process tree
    and mark it stopped."""
    job = jobs.get(job_id)
    if not job:
        abort(404)
    if job["status"] != "running":
        abort(400, "job is not running")
    with jobs_lock:
        job["status"] = "stopped"
        job["lines"].append("⏹ stopped by user")
    pid = job.get("pid")
    if pid:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    job["ended"] = time.time()
    job["returncode"] = -9
    return jsonify({"stopped": job_id})


@jobs_bp.post("/api/job/<job_id>/resume")
def api_job_resume(job_id):
    """Re-run an interrupted/failed/stopped job with its recorded
    command. Engines with checkpoint caches (ProPainter erase,
    whisper words.json) pick up where they left off.

    Wire format: byte-equivalent to server.py L365-391 — the same
    4 validation gates (404 missing, 400 wrong state, 400 predates
    resume, 400 cloud-dub blind-resume), the same ``jobs[new_id]``
    record shape, and the same ``{job_id, resumed_from}`` response.

    Spawn: bare ``JobRunner.run()`` bound method on a daemon thread,
    no wrapper. This is the same recipe ``routes/captions.py:336``
    (the first bare caller, B8), ``routes/brand.py:241`` (B13 S5)
    and ``routes/dubsync.py:213`` (B11) already use. Resume is the
    one route that does NOT need a worker wrapper because:

      - no cost recording — cloud dubs are blocked above, and local
        dubs' cost is already booked the first time around
      - no LLM call — resume is a mechanical subprocess re-spawn
      - no special pre/post — ``JobRunner.run`` already handles the
        GPU lock, status mapping, and the fal.ai post-failure hints

    ``runner.run`` is a bound method, so it carries the runner's
    ``_cwd`` (the autoVSL repo root) and ``_job_env_factory`` (the
    Gyan ffmpeg PATH + PYTHONUTF8=1) with it — the same way
    ``services/llm.py:355``'s ``self.run_chat_turn`` does. Rule 8.5
    is honored: ``current_app`` is read at request time, the worker
    thread itself never touches it.

    The validation block below is byte-equivalent to server.py
    L370-381 — preserved exactly so the external contract is
    unchanged (the same 4xx codes fire before the spawn).
    """
    job = jobs.get(job_id)
    if not job:
        abort(404)
    if job["status"] not in ("interrupted", "failed", "stopped"):
        abort(400, "only interrupted/failed/stopped jobs can be resumed")
    cmd = job.get("cmd")
    if not cmd:
        abort(400, "this job predates resume support (no command recorded)")
    # money stays gated: cloud dubs re-charge on every run, so they
    # must go back through the Dubbing tab's cost-confirmation flow
    if any(part.endswith("dub.py") and not part.endswith("local_dub.py") for part in cmd):
        abort(400, "cloud dubs can't be blind-resumed — re-run from the Dubbing tab so the cost is confirmed")
    new_id = uuid.uuid4().hex[:8]
    jobs[new_id] = {
        "id": new_id, "action": job.get("action"), "slug": job.get("slug"),
        "label": f"{job.get('label') or job.get('action') or 'job'} (resumed)",
        "status": "running", "lines": [f"▶ resuming job {job_id}"],
        "returncode": None, "started": time.time(), "ended": None,
        "resumed_from": job_id,
    }
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(
        target=runner.run, args=(new_id, cmd), daemon=True,
    ).start()
    return jsonify({"job_id": new_id, "resumed_from": job_id})


def register_jobs(app) -> None:
    """Register the jobs route module on ``app``."""
    app.register_blueprint(jobs_bp)
