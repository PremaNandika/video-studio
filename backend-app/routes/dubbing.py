"""Dubbing route module — POST /api/run/dub + its worker.

Pure move of two blocks from video-studio/app/server.py:

  1. The ``if action == "dub":`` branch of ``/api/run``
     (server.py L497-563, ~67 lines) — moved to ``dub_action()``.
  2. The ``run_dub_job()`` worker (server.py L394-419, ~26 lines)
     — moved to ``dub_worker()``.

Plus 4 module-level constants (lipsync/tier/tts allow-lists) and
1 module function (``_busy_dub_job`` for the 409 check).

ROUTE LAYOUT — WHY A NEW URL INSTEAD OF REUSING /api/run
  The old server.py has ``POST /api/run`` as a dispatcher:
  the body's ``action`` field picks the branch
  (``"dub" | "transcribe" | "caption" | "recaption" | ...``).
  The new app exposes the dub action as its own self-
  documenting URL: ``POST /api/run/dub`` — no ``action``
  field needed, the URL already says what it is.

  Why split the URL? Two reasons:
    (a) The other branches of /api/run (transcribe, caption,
        recaption, the shell-script actions) haven't moved
        yet. A new app that registers ``POST /api/run``
        would have to dispatch every branch, with stubs for
        the un-moved ones. Better: B7 ships the dub
        endpoint; B8 ships ``POST /api/run/caption``; B9
        ships ``POST /api/run/erase``; etc. When all
        branches are moved, a future commit can introduce
        a unified ``POST /api/run`` that dispatches via
        a small action table.
    (b) The B5/B6 pattern is "one Blueprint per blueprint,
        each owns its routes" — no shared dispatchers.
        B7 follows that pattern.

  The old server.py keeps its ``POST /api/run`` unchanged.
  Both apps run side by side; the frontend picks which URL
  to hit.

WHAT IS NOT HERE (deferred to B7c/final cutover + B10 clone):
  - The actual deletion of the dub branch from server.py.
    Rule 16 keeps the old code in place until the *entire*
    dub/caption/recaption/transcribe dispatcher is retired.
  - The clone-winner dub call (server.py L3117) still uses
    the old ``run_dub_job``. B10 clone will replace it
    with a call to ``dub_worker`` (the function below).
    The signature is byte-compatible so the swap is a
    one-line change.
  - The bash-script / generate-video / assemble actions of
    ``/api/run`` (B13 Ads Factory's catch-all). Out of scope.

DEPENDENCIES:
  - services.jobs:        jobs dict + jobs_lock (the in-memory store)
  - services.spend:       SpendLedger (record_dub_run + load cloned_stems)
  - services.job_runner:  JobRunner (the shared engine subprocess runner)
  - services.workdir:     DubWorkdir (the script_edited path check)

LIFECYCLE (per Rule 8.1):
  - ``dub_action`` is a module function called by
    ``api_run_dub`` (the route handler). No Flask globals
    leak; everything is read from ``current_app.config``.
  - ``dub_worker`` is the thread target; it receives
    ``runner`` and ``ledger`` as explicit args (Rule 8.5:
    no cross-tree imports in worker threads, pass
    dependencies explicitly).
  - The Blueprint is registered via ``register_dubbing(app)``
    in the app factory.

server.py is unchanged. The dub branch of ``/api/run`` +
``run_dub_job`` stay at their original lines until the B7c
cutover lands. Rule 16.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.jobs import jobs, jobs_lock
from services.workdir import DubWorkdir


# ---------------------------------------------------------------- constants
# Allow-lists for the lipsync / tier / tts body fields. server.py uses
# inline tuples; we promote them to module constants for readability
# (they're 8-3 long, and the inline form made the function walls of
# text).

_LOCAL_LIPSYNC = ("none", "wav2lip", "wav2lip-hd", "latentsync",
                  "musetalk", "veed", "standard", "pro")
_PAID_LIPSYNC = ("latentsync", "musetalk", "veed", "standard", "pro")
_FAL_TIER = ("pro", "standard", "veed", "latentsync", "musetalk")
_FAL_TTS = ("hd", "turbo", "f5")


# ---------------------------------------------------------------- module helpers

def _busy_dub_job() -> dict | None:
    """Return the currently-running dub job, or None.

    Used to enforce the one-dub-at-a-time rule (concurrent
    XTTS/Wav2Lip jobs OOM the 4GB GPU and all fail). The
    409 response includes the busy job's slug so the user
    knows which one to wait for.
    """
    with jobs_lock:
        return next((j for j in jobs.values()
                     if j["action"] == "dub" and j["status"] == "running"),
                    None)


# ---------------------------------------------------------------- HTTP route

dub_bp = Blueprint("dubbing", __name__)


@dub_bp.post("/api/run/dub")
def api_run_dub():
    """The new app's dub endpoint. Thin wrapper around ``dub_action``.

    Matches the B5/B6 pattern: every blueprint owns a Blueprint
    with HTTP routes; the new app gets the same capability as
    the old app but under a self-documenting URL. The old
    ``server.py`` keeps its ``POST /api/run`` dispatcher (and
    its ``action=="dub"`` branch) — both apps run side by
    side, the frontend picks which one to hit.

    Request body (JSON): the same fields the legacy dub action
    accepts — ``file``, ``engine`` (``"local"`` | ``"fal"``),
    ``lipsync``, ``tier``, ``tts``, ``language``, ``keep_volume``,
    ``captions``, ``confirm_cost``. No ``action`` field needed —
    the URL already says this is a dub.

    Response: ``{"job_id": "..."}`` on success, or 4xx with a
    JSON error body via the app-level error handlers
    (registered in ``register_api_errors``).
    """
    body = request.get_json(force=True) or {}
    return dub_action(body)


# ---------------------------------------------------------------- public action

def dub_action(body: dict):
    """The ``if action == "dub":`` branch of /api/run.

    Validates the request, builds the engine command + cost context,
    creates the job record, and spawns a daemon thread that calls
    ``dub_worker``.

    Pure relocation of server.py L497-563. The body is byte-
    equivalent; the only deltas are:
      - ``UPLOADS`` (module global) → ``current_app.config["UPLOADS"]``
      - ``SWAP_WORK`` (module global) → ``DubWorkdir(...).dir``
      - ``CONFIG["venvs"]["cv"]`` (module global) →
        ``current_app.config["CV_VENV_PY"]``
      - ``ENGINES / "local_dub.py"`` (module global) →
        ``current_app.config["ENGINES_DIR"] / "local_dub.py"``
      - ``run_dub_job`` (server.py helper) → ``dub_worker`` (here)
      - The worker thread receives ``runner`` and ``ledger`` from
        ``app.config`` via the route module's
        ``register_dubbing`` wire-up. (Rule 8.5.)

    Returns:
        Flask ``jsonify({"job_id": "..."})`` response.
    """
    fname = Path(body.get("file", "")).name
    uploads = current_app.config["UPLOADS"]
    src = uploads / fname
    if not fname or not src.is_file():
        abort(400, "file not found in uploads/")
    stem = src.stem
    autovsl = current_app.config["AUTOVSL_ROOT"]
    if not (DubWorkdir(autovsl, stem).script_edited).is_file():
        abort(400, "no edited script saved yet — use Edit script first")

    # one dub at a time — concurrent XTTS/Wav2Lip jobs OOM the 4GB GPU and all fail
    busy = _busy_dub_job()
    if busy:
        abort(409, f"a dub is already running ({busy['slug']}) — the GPU handles one at a "
                   "time; wait for it to finish, then start the next")

    engine = body.get("engine") if body.get("engine") in ("local", "fal") else "fal"
    venv_py = Path(current_app.config["CV_VENV_PY"])
    engines_dir = current_app.config["ENGINES_DIR"]

    if engine == "local":
        # local XTTS voice (free); lip-sync "none"/"wav2lip" are free (local GPU),
        # any fal tier costs money
        lipsync = body.get("lipsync") if body.get("lipsync") in _LOCAL_LIPSYNC else "none"
        paid = lipsync in _PAID_LIPSYNC
        if paid and not body.get("confirm_cost"):
            abort(400, f"lip-sync '{lipsync}' runs on fal.ai and costs money — needs cost approval (confirm_cost)")
        cmd = [str(venv_py), str(engines_dir / "local_dub.py"), str(src),
               "--name", stem, "--lipsync", lipsync]
        lang = str(body.get("language") or "en")[:5]
        if lang.replace("-", "").isalpha():
            cmd += ["--language", lang]
        try:
            keepvol = max(0.0, min(1.0, float(body.get("keep_volume") or 0.0)))
        except (TypeError, ValueError):
            keepvol = 0.0
        if keepvol:
            cmd += ["--keep-volume", str(keepvol)]
        label = (f"Local dub — {fname} (XTTS voice"
                 + (", free)" if not paid else f" + {lipsync} lip-sync $)"))
        cost_ctx = {"engine": "local", "tts": "local", "tier": lipsync,
                    "video": str(src), "stem": stem, "paid": paid}
    else:
        # cloud pipeline: always costs money
        if not body.get("confirm_cost"):
            abort(400, "FAL.AI dub spends money (voice-clone + TTS + lip-sync) — needs cost approval (confirm_cost)")
        tier = body.get("tier") if body.get("tier") in _FAL_TIER else "pro"
        tts = body.get("tts") if body.get("tts") in _FAL_TTS else "hd"
        cmd = [str(venv_py), str(engines_dir / "dub.py"), str(src),
               "--name", stem, "--tier", tier, "--tts", tts]
        if body.get("captions", True):
            cmd.append("--captions")
        label = f"FAL.AI dub — {fname} (voice:{tts}, sync:{tier}) $"
        cost_ctx = {"engine": "fal", "tts": tts, "tier": tier,
                    "video": str(src), "stem": stem, "paid": True}

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "action": "dub", "slug": stem,
        "label": label,
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
    }
    runner = current_app.config["JOB_RUNNER"]
    ledger = current_app.config["SPEND_LEDGER"]
    threading.Thread(
        target=dub_worker, args=(job_id, cmd, cost_ctx, runner, ledger),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# ---------------------------------------------------------------- worker thread

def dub_worker(job_id: str, cmd: list[str], cost_ctx: dict,
               runner, ledger) -> None:
    """Run a dub, then (if it spent money) append a cost line + update the running total.

    Pure relocation of server.py L394-419. The function
    delegates the subprocess work to ``runner.run()`` (B7a
    service) and the cost recording to ``ledger.estimate_dub_cost``
    + ``ledger.record`` (S3 service).

    Behavior (unchanged from the original):
      1. Run the engine subprocess via ``runner.run()``.
      2. If the job didn't reach ``"done"`` status (failed,
         stopped, or interrupted), bail — no spend recorded
         for failed runs.
      3. Estimate the cost from the engine/tts/tier/video/stem
         tuple + the cloned_stems cache.
      4. If ``paid`` and the estimated cost > 0: record to
         the spend ledger, append a 💰 cost line + a 🧾
         running-total line to the job log, and stash the
         cost summary on the job dict.
      5. If the cost estimate is free (local engine), append
         a ✅ free line and stash the free summary.
      6. Any exception during cost tracking is logged but
         doesn't take the job down.
    """
    from services.jobs import jobs_lock  # local: explicit dependency
    runner.run(job_id, cmd)
    job = jobs[job_id]
    if job["status"] != "done":
        return
    try:
        info = ledger.estimate_dub_cost(
            cost_ctx["engine"], cost_ctx["tts"], cost_ctx["tier"],
            Path(cost_ctx["video"]), cost_ctx["stem"],
            ledger.load().get("cloned_stems", {}),
        )
        if cost_ctx.get("paid") and info["this_run"] > 0:
            res = ledger.record(cost_ctx["stem"], info)
            with jobs_lock:
                job["lines"].append("")
                job["lines"].append(f"💰 This dub cost ~${res['this_run']:.3f} on fal.ai  ({info['summary']})")
                job["lines"].append(f"🧾 Total spent on fal.ai so far: ${res['total']:.2f}")
            job["cost"] = {"this_run": res["this_run"], "total": res["total"], "summary": info["summary"]}
        else:
            with jobs_lock:
                job["lines"].append("")
                job["lines"].append(f"✅ This dub was FREE (local) — {info['summary']}")
            job["cost"] = {"this_run": 0.0, "total": ledger.load().get("total", 0.0),
                           "summary": info["summary"], "free": True}
    except Exception as exc:
        with jobs_lock:
            job["lines"].append(f"(cost tracking skipped: {exc})")


# ---------------------------------------------------------------- wire-up

def register_dubbing(app) -> None:
    """Register the dubbing Blueprint on ``app`` and stash
    the CV venv path on ``app.config["CV_VENV_PY"]``.

    The Blueprint owns one route: ``POST /api/run/dub``. The
    route is a thin wrapper over ``dub_action`` (the relocated
    dub action body from server.py L497-563).

    The CV venv path is derived from
    ``app.config["AUTOVSL_ROOT"]`` + the standard
    ``.venv/Scripts/python.exe`` layout, mirroring the legacy
    default ``CONFIG["venvs"]["cv"]``. A future commit can
    promote this to an explicit config.json key if the user
    ever needs to point the dub at a different venv.
    """
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["CV_VENV_PY"] = str(autovsl / ".venv" / "Scripts" / "python.exe")
    app.register_blueprint(dub_bp)
