"""Subtitles route module — erase burned-in subtitle regions.

Pure move of three blocks from video-studio/app/server.py:

  1. ``_clean_request()`` helper (server.py L861-876, ~16 lines)
     — parses the JSON body, validates the box, picks the mode.
     Moved to ``_clean_request()`` (unchanged signature).
  2. ``POST /api/clean-preview`` route (server.py L879-901, ~23 lines)
     — runs the subtitle engine on a single frame so the user
     can preview the chosen method BEFORE committing. Moved to
     ``api_clean_preview()`` (same URL).
  3. ``POST /api/clean-subs`` route (server.py L904-912, ~9 lines)
     — starts the real subtitle-clean job. The job body itself
     is the long ``clean_subs_worker()`` function (server.py
     L775-858, ~84 lines) — moved to ``clean_subs_worker()``
     (unchanged signature).
  4. ``POST /api/clean-restore`` route (server.py L952-964, ~13 lines)
     — undoes a subtitle clean by moving the backed-up original
     back as the source. Moved to ``api_clean_restore()`` (same URL).

URL CHOICE — SAME URLS AS LEGACY
  All three routes keep their original URLs (``/api/clean-preview``,
  ``/api/clean-subs``, ``/api/clean-restore``). The legacy server.py
  keeps all three unchanged — both apps run side by side, the
  frontend picks which one to hit.

  Why no URL-namespace split (like B7's ``/api/run/dub``)?
  The legacy URLs are already self-documenting — they say
  "clean" in the path, no ``action`` field to dispatch on.
  Reusing them keeps the frontend (and any bookmarks / log
  greps) working without changes.

WHAT IS NOT HERE (deferred to other blueprints):
  - ``POST /api/recaption-from-product`` (server.py L2341ish) —
    recaption + LLM aifix combo. B12 Chat scope.
  - The actual deletion of the clean-* blocks from server.py
    (B9c cutover). Deferred until the rest of the subtitle
    subsystem (transcribe, recaption-from-product) is moved.
  - The QC tab's /api/qc/* routes. B14.

DEPENDENCIES:
  - services.jobs:        jobs dict + jobs_lock (the in-memory store)
  - services.job_runner:  JobRunner (the shared engine subprocess runner)
  - services.workdir:     DubWorkdir (the final.mp4 check after clean)
  - services.spend:       read_json (for the box.json loader — wait, no,
                           box.json is written by the worker, not loaded
                           by a route; deferred to 3rd-use rule)

LIFECYCLE (per Rule 8.1):
  - The routes read paths from ``current_app.config`` in the
    REQUEST context: ``UPLOADS``, ``AUTOVSL_ROOT`` (for
    DubWorkdir), ``CV_VENV_PY`` (stashed by register_dubbing),
    ``ERASE_PY`` + ``FFMPEG_BIN`` (stashed by
    ``register_subtitles``), ``ENGINES_DIR`` (stashed by app.py).
  - The worker thread receives the ``runner`` AND a resolved
    ``paths`` bundle as explicit args (Rule 8.5: no cross-tree
    imports in worker threads, pass dependencies explicitly).
    The worker MUST NOT touch ``current_app`` — it runs on a
    bare daemon thread with no application context, so a
    ``current_app`` read there raises RuntimeError. All config
    is resolved by the route (which has a context) and handed
    down. Same rule as B7's ``dub_worker`` / B8's ``_spawn_job``.
  - The engine subprocess is launched via ``runner.run()``
    (B7a service) — not via the worker's own Popen — so the
    GPU lock + cwd + job_env factory stay in one place.
  - ``register_subtitles(app)`` is the wire-up point. It
    computes and stashes ``ERASE_PY`` and ``FFMPEG_BIN`` on
    ``app.config`` (it does NOT touch ``UPLOADS`` or
    ``CV_VENV_PY`` — those derivations are owned by other
    register_* helpers).

server.py is unchanged. The clean-* blocks + clean_subs_worker
stay at their original lines until the B9c cutover lands. Rule 16.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from services.jobs import jobs, jobs_lock
from services.workdir import DubWorkdir


# ---------------------------------------------------------------- module helpers

def _clean_request():
    """Parse + validate a /api/clean-* body.

    Pure relocation of server.py L861-876. The body is byte-
    equivalent; the only delta is that ``UPLOADS`` (a server.py
    module global) becomes ``current_app.config["UPLOADS"]``.

    Returns the (body, fname, src, box, mode) tuple every
    clean-* handler consumes. The box is None for the one-
    click ``auto`` mode (the engine finds the caption band
    itself).
    """
    body = request.get_json(force=True)
    fname = Path(body.get("file") or "").name
    src = current_app.config["UPLOADS"] / fname
    if not fname or not src.is_file():
        abort(404, "upload not found")
    if body.get("auto"):        # one-click: auto-detect the caption band, AI-erase it
        return body, fname, src, None, "erase"
    try:
        box = {k: int(body[k]) for k in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError):
        abort(400, "need integer x/y/w/h box")
    if box["w"] < 4 or box["h"] < 4:
        abort(400, "box too small — drag a rectangle over the subtitles")
    mode = body.get("mode") if body.get("mode") in ("smart", "blur", "bar", "erase") else "smart"
    return body, fname, src, box, mode


# ---------------------------------------------------------------- HTTP routes

subtitles_bp = Blueprint("subtitles", __name__)


# -- 1. POST /api/clean-preview (single-frame preview) -------------------

@subtitles_bp.post("/api/clean-preview")
def api_clean_preview():
    """Render one processed frame so the user can judge the method
    BEFORE committing.

    Body (JSON): ``{file, auto?, x, y, w, h, mode?, t?}`` (see
    ``_clean_request``). The response is the rendered JPEG as
    ``image/jpeg``; ``send_file`` handles the ``max_age=0`` cache
    bust.

    Pure relocation of server.py L879-901. The engine call is a
    synchronous ``subprocess.run`` with a 90 s timeout (it's a
    single frame, not the full video) — not a JobRunner task.
    """
    body, fname, src, box, mode = _clean_request()
    t = max(0.0, float(body.get("t") or 1.0))
    out = current_app.config["UPLOADS"] / f".preview-{Path(fname).stem}.jpg"
    venv_py = Path(current_app.config["CV_VENV_PY"])
    if mode == "erase":
        # approximate single-frame preview (text mask + spatial inpaint) — the real run
        # uses ProPainter video inpainting, which fills noticeably better than this
        cmd = [str(venv_py), str(current_app.config["ERASE_PY"]), str(src), str(out),
               "--x", str(box["x"]), "--y", str(box["y"]),
               "--w", str(box["w"]), "--h", str(box["h"]), "--preview-at", str(t)]
    else:
        cmd = [str(venv_py), str(current_app.config["ENGINES_DIR"] / "subclean.py"), str(src),
               "--box", str(box["x"]), str(box["y"]), str(box["w"]), str(box["h"]),
               "--mode", mode, "--preview-at", str(t), "--out", str(out)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=current_app.config["JOB_RUNNER"]._job_env_factory(), timeout=90)
    if proc.returncode != 0 or not out.is_file():
        abort(500, f"preview failed: {(proc.stdout or '')[-200:]}")
    return send_file(out, mimetype="image/jpeg", max_age=0)


# -- 2. POST /api/clean-subs (kick off the real clean job) ---------------

@subtitles_bp.post("/api/clean-subs")
def api_clean_subs():
    """Start the real subtitle-clean job (ProPainter or subclean).

    Body (JSON): same as ``/api/clean-preview``. Response:
    ``{"job_id": "..."}`` — the worker streams progress via
    the standard job-log polling endpoints (B3).

    Pure relocation of server.py L904-912. The job dict shape
    matches B7/B8 exactly (id, action, slug, label, status,
    lines, returncode, started, ended) so the jobs route module
    can poll it without special-casing.
    """
    body, fname, src, box, mode = _clean_request()
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "clean-subs", "slug": fname,
                    "label": f"Remove subtitles — {fname} ({mode})", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    runner = current_app.config["JOB_RUNNER"]
    # Resolve every config value HERE, in the request context, and pass it to
    # the worker as an explicit bundle. The worker runs on a bare daemon
    # thread with NO Flask application context — reading current_app.config
    # inside it raises RuntimeError("Working outside of application context").
    # This mirrors the B7 (dub_worker) / B8 (_spawn_job) rule: worker threads
    # never touch current_app; they receive their dependencies as args.
    paths = {
        "uploads": current_app.config["UPLOADS"],
        "autovsl": current_app.config["AUTOVSL_ROOT"],
        "ffmpeg_bin": current_app.config["FFMPEG_BIN"],
        "cv_venv_py": current_app.config["CV_VENV_PY"],
        "erase_py": current_app.config["ERASE_PY"],
        "engines_dir": current_app.config["ENGINES_DIR"],
    }
    threading.Thread(
        target=clean_subs_worker, args=(job_id, fname, box, mode, runner, paths),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


# -- 3. POST /api/clean-restore (undo a clean) ---------------------------

@subtitles_bp.post("/api/clean-restore")
def api_clean_restore():
    """Undo a subtitle clean — put the backed-up original back as the source.

    Body (JSON): ``{file: "<name>"}``. The file must have a
    backup at ``uploads/.originals/<name>``; aborts 404 otherwise.
    Also removes the side-car ``.originals/<stem>.box.json`` (the
    box.json the worker wrote so the captions tab knows where
    to burn the new captions).

    Pure relocation of server.py L952-964. The body is byte-
    equivalent; the only delta is that ``UPLOADS`` (a server.py
    module global) becomes ``current_app.config["UPLOADS"]``.
    """
    fname = Path(request.get_json(force=True).get("file") or "").name
    backup = current_app.config["UPLOADS"] / ".originals" / fname
    if not fname or not backup.is_file():
        abort(404, "no backup for that video")
    src = current_app.config["UPLOADS"] / fname
    if src.exists():
        src.unlink()
    shutil.move(str(backup), str(src))
    (current_app.config["UPLOADS"] / ".originals" / f"{Path(fname).stem}.box.json").unlink(missing_ok=True)
    return jsonify({"restored": fname})


# ---------------------------------------------------------------- worker thread

def clean_subs_worker(job_id: str, fname: str, box, mode: str, runner, paths) -> None:
    """Clean a burned-in subtitle region (ProPainter AI or OpenCV per-frame);
    the original is backed up to ``uploads/.originals/``.

    Pure relocation of server.py L775-858. The body is byte-
    equivalent; the deltas are:

      - ``ffmpeg_exe("ffprobe")`` → ``paths["ffmpeg_bin"] / "ffprobe.exe"``
        (with the same "fall back to bare name" behavior so the path is
        robust on machines where the Gyan WinGet install isn't present).
      - ``UPLOADS`` / ``SWAP_WORK`` / ``CONFIG["venvs"]["cv"]`` /
        ``ERASE_PY`` / ``ENGINES`` → values read from the ``paths`` bundle
        the route resolved (see below). This worker runs on a bare daemon
        thread with NO application context, so it MUST NOT touch
        ``current_app`` — the route resolves the config and passes it in
        (same rule as B7's ``dub_worker`` and B8's ``_spawn_job``).
      - The engine subprocess is now ``runner.run(job_id, cmd)`` (the
        B7a JobRunner service) — instead of a bare ``subprocess.Popen``
        with ``env=job_env()``. ``runner.run`` does the Popen + env
        + cwd + GPU lock + stdout streaming + returncode/status
        mutation, so the worker body is just preprocessing
        (ffprobe) + the engine call + postprocessing (rename,
        .originals backup, box.json write). This is the
        spirit-of-B7a move: the new app's engine subprocess
        path is the same as every other blueprint.

    The worker's pre/post is the only thing that doesn't fit the
    "spawn engine, stream output" pattern. It:
      1. ffprobes the source to get the video width/height (clamping
         the box to the frame).
      2. Calls ``runner.run(job_id, cmd)`` to run the engine. The
         runner streams the engine's stdout into ``job["lines"]``
         and sets ``job["returncode"]`` + ``job["status"]``.
      3. If the engine succeeded: moves the source to
         ``.originals/`` (if no backup yet), recovers the
         auto-detected box from the engine's log if it ran in
         auto mode, writes the per-stem ``.box.json`` side-car
         (so the captions tab knows where to burn new captions),
         then renames the temp into place over the source.
      4. If the engine failed: logs the failure, marks the job
         failed (unless the user pressed Stop), and removes
         the ``.cleaning.<ext>`` temp file.
      5. In all cases: stamps ``job["ended"]`` in the finally block.
    """
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        uploads = paths["uploads"]
        autovsl = paths["autovsl"]
        src = uploads / fname
        # ffprobe the source for the video dims (needed to clamp the box).
        # Mirrors the legacy ffmpeg_exe("ffprobe") behavior: use the Gyan
        # path if present, else fall back to the bare "ffprobe" name.
        ffprobe_exe = paths["ffmpeg_bin"] / "ffprobe.exe"
        ffprobe = (str(ffprobe_exe) if ffprobe_exe.is_file() else "ffprobe")
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, check=True)
        vw, vh = (int(n) for n in probe.stdout.strip().split(",")[:2])
        tmp = uploads / f"{src.stem}.cleaning{src.suffix}"
        venv_py = Path(paths["cv_venv_py"])
        if box is None:
            # one-click mode: erase_subs.py finds the caption band itself (ProPainter AI fill)
            x = y = w = h = None
            log(f"Auto-detecting the caption region of {vw}x{vh} — mode: erase (AI inpaint, audio untouched)")
            cmd = [str(venv_py), str(paths["erase_py"]), str(src), str(tmp)]
        elif mode == "erase":
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            # AI video inpainting (ProPainter) — reconstructs the real background behind
            # the letters from neighboring frames; slow (GPU, minutes) but the best fill
            cmd = [str(venv_py), str(paths["erase_py"]), str(src), str(tmp),
                   "--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
        else:
            x = max(0, min(int(box["x"]), vw - 4))
            y = max(0, min(int(box["y"]), vh - 4))
            w = max(4, min(int(box["w"]), vw - x))
            h = max(4, min(int(box["h"]), vh - y))
            log(f"Cleaning {w}x{h} region at ({x},{y}) of {vw}x{vh} — mode: {mode} (audio untouched)")
            cmd = [str(venv_py), str(paths["engines_dir"] / "subclean.py"), str(src),
                   "--box", str(x), str(y), str(w), str(h), "--mode", mode, "--out", str(tmp)]

        # Engine subprocess: the JobRunner handles Popen + env + cwd + GPU
        # lock + stdout streaming + returncode/status mutation. The worker
        # only needs to know "did it succeed?" via job["status"] afterward.
        # NOTE: this also takes the GPU_LOCK for any GPU engine call
        # (ProPainter's erase_subs.py matches GPU_MARKERS) — a strict
        # improvement over the legacy code which had no GPU arbitration
        # for clean-subs. Concurrent clean-subs jobs on the same card
        # used to OOM; now they serialize.
        runner.run(job_id, cmd)

        # If the engine itself failed (the runner sets status="failed"),
        # do the same on-disk cleanup the legacy except clause did:
        # log a "CLEAN FAILED: ..." line and unlink the .cleaning.<ext>
        # temp file. The legacy code did `log(f"CLEAN FAILED: {exc}")`
        # where exc was a RuntimeError("subtitle cleaner failed — see
        # log above") for the engine-exit-1 case, or the actual exception
        # for FileNotFoundError / OOM. The runner's behavior is:
        #   - Engine exits non-zero: runner does NOT raise, just sets
        #     status="failed". The worker's `if status == failed:` block
        #     must add the CLEAN FAILED line ourselves (the old code
        #     raised a RuntimeError to trigger the except clause).
        #   - Engine raises (FileNotFoundError etc.): runner catches in
        #     its own `except` and appends "[dashboard] failed to run: <exc>"
        #     to job["lines"]. To preserve the legacy log line shape,
        #     we rewrite that prefix to "CLEAN FAILED: ".
        # This block is the minimum needed to keep the user-visible
        # log text byte-equivalent with the legacy code on the failure path.
        if job["status"] == "failed":
            with jobs_lock:
                last = job["lines"][-1] if job["lines"] else ""
            if last.startswith("[dashboard] failed to run: "):
                rewritten = "CLEAN FAILED: " + last[len("[dashboard] failed to run: "):]
                with jobs_lock:
                    job["lines"][-1] = rewritten
            else:
                log("CLEAN FAILED: subtitle cleaner failed — see log above")
            (uploads / f"{Path(fname).stem}.cleaning{Path(fname).suffix}").unlink(missing_ok=True)
            return

        # If the user stopped the job, the runner already set job["status"]
        # to "stopped" and the engine was killed. The source was not modified
        # (the tmp.rename(src) below never ran), so we're done.
        if job["status"] == "stopped":
            return

        # DEFENSIVE GUARD (restored from legacy server.py L822):
        #   if proc.returncode != 0 or not tmp.is_file(): raise ...
        # The runner keys job["status"] on the return code ALONE, so an
        # engine that exits 0 without writing the tmp output (a real
        # ProPainter failure mode — NaN/black-frame retry exhaustion)
        # arrives here as status="done". Without this check the worker
        # would move the source into .originals/ and then crash at
        # tmp.rename(src), leaving the upload missing on a "failed" job.
        # Legacy code raised BEFORE touching .originals, keeping the
        # source intact. We reproduce that: fail cleanly, source untouched.
        # The log text matches the legacy RuntimeError message exactly.
        if not tmp.is_file():
            log("CLEAN FAILED: subtitle cleaner failed — see log above")
            job["returncode"] = 1
            job["status"] = "failed"
            (uploads / f"{Path(fname).stem}.cleaning{Path(fname).suffix}").unlink(missing_ok=True)
            return

        # The engine wrote tmp — the real run was successful. The runner set
        # job["status"] = "done" and job["returncode"] = 0. Proceed to
        # post-processing.

        originals = uploads / ".originals"
        originals.mkdir(exist_ok=True)
        if not (originals / fname).exists():
            shutil.move(str(src), str(originals / fname))
            log(f"Original backed up to uploads/.originals/{fname}")
        else:
            src.unlink()  # already have the first original — this was a re-clean
        # remember the cleaned band so captions can be burned exactly over it later
        if x is None:  # auto mode — recover the box erase_subs detected from its log
            with jobs_lock:
                joined = "\n".join(job["lines"])
            m = re.search(r"captions detected at \((\d+),(\d+)\) (\d+)x(\d+)", joined)
            if m:
                x, y, w, h = (int(g) for g in m.groups())
        if x is not None:
            (originals / f"{Path(fname).stem}.box.json").write_text(
                json.dumps({"x": x, "y": y, "w": w, "h": h, "vw": vw, "vh": vh, "mode": mode}),
                encoding="utf-8")
        tmp.rename(src)
        log("Source replaced with the cleaned video.")
        if DubWorkdir(autovsl, src.stem).final.is_file():
            log("NOTE: an existing dub used the old (subtitled) video — re-dub to refresh it.")
    except Exception as exc:
        if job["status"] == "stopped":
            log("clean cancelled — the source video was NOT modified")
        else:
            log(f"CLEAN FAILED: {exc}")
            job["returncode"] = 1
            job["status"] = "failed"
        (paths["uploads"] / f"{Path(fname).stem}.cleaning{Path(fname).suffix}").unlink(missing_ok=True)
    finally:
        job["ended"] = time.time()


# ---------------------------------------------------------------- wire-up

def register_subtitles(app) -> None:
    """Register the subtitles Blueprint on ``app`` and stash
    the two config keys the clean-* routes + worker need.

    Two config keys are set:
      - ``ERASE_PY`` — the ProPainter-backed ``erase_subs.py``
        engine script (the AI inpaint path for "erase" mode).
        Mirrors server.py's ``ERASE_PY = CONFIG["engines"]["erase"]``.
      - ``FFMPEG_BIN`` — the absolute path to the directory
        containing ``ffmpeg.exe`` / ``ffprobe.exe``. The clean
        worker calls ``ffprobe`` for the video dims, and the
        Gyan WinGet install path is the only place that has
        it (mirrors server.py's module-level ``FFMPEG_BIN``).

    Both paths are derived from standard locations on the
    dev box. The ffmpeg derivation matches the
    ``_default_job_env`` factory in ``app.py`` so the GPU
    job's PATH and the worker's ffprobe path agree.

    NOTE: this function does NOT touch ``CV_VENV_PY`` or
    ``UPLOADS`` or ``ENGINES_DIR`` — those derivations are
    owned by other register_* helpers. Register order
    (dubbing before subtitles) ensures ``CV_VENV_PY`` is
    set when the clean routes are called.
    """
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    # ERASE_PY: matches server.py L55 `ERASE_PY = Path(CONFIG["engines"]["erase"])`.
    # The default points at the subtitle-studio engine — same physical file
    # the legacy app uses. Mirrors the B7/B8 register_* pattern of deriving
    # a path from AUTOVSL_ROOT (e.g. register_captions derives RECAPTION_PY
    # the same way). The config.json "engines.erase" key currently has the
    # same effective value; a future commit can promote it to a real
    # config read if the user ever needs to point ERASE_PY elsewhere.
    app.config["ERASE_PY"] = str(
        autovsl / ".." / "subtitle-studio" / "erase_subs.py"
    )
    # FFMPEG_BIN: matches server.py L92-95 `FFMPEG_BIN = LOCALAPPDATA / Gyan...`.
    # The previews/workers call ffprobe; the rest of the system prepends
    # the same dir to PATH via _default_job_env in app.py. Keeping the
    # derivation here (rather than reading from cfg["ffmpeg"]) matches
    # the established B7/B8 register_* style and keeps the path-resolution
    # logic local to the module that needs it.
    ffmpeg_bin = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft/WinGet/Packages"
        / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        / "ffmpeg-8.1.2-full_build/bin"
    )
    app.config["FFMPEG_BIN"] = ffmpeg_bin
    app.register_blueprint(subtitles_bp)
