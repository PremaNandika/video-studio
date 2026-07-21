"""Clone Winner route module — scale a proven ad into fresh variants.

Pure move of the Clone Winner subsystem from
video-studio/app/server.py L2902-3143:

  1. ``GET  /api/clone/winners`` (L2949-2973) — finished dubs that can
     serve as a winning template (have a final.mp4 + a script). Moved
     to ``api_clone_winners()`` (same URL).
  2. ``GET  /api/clone/actors``  (L2976-2987) — every uploads/ video,
     offered as alternate actor footage. Moved to ``api_clone_actors()``.
  3. ``POST /api/clone/script``  (L3006-3051) — generate a new
     winning-pattern script sized to the chosen actor footage. Makes a
     synchronous Claude CLI call (see LLM note below). Moved to
     ``api_clone_script()`` (same URL).
  4. ``POST /api/clone/run``     (L3054-3118) — create the clone workdir
     and run the full dub chain on it (same GPU/cost gates as Dubbing).
     Moved to ``api_clone_run()`` (same URL).
  5. ``GET  /api/clone/list``    (L3121-3143) — every clone made so far,
     with its state (running / ready / failed). Moved to
     ``api_clone_list()`` (same URL).

Plus 4 helpers moved / adapted from server.py:
  - ``_winner_script(wd)``      (L2939-2946) — first non-empty of
    script-edited.txt / transcript.txt. Now takes a ``DubWorkdir``.
  - ``_clone_actor_video(wd, actor, uploads)`` (L2990-3003) — resolve
    the actor choice ("same" → the winner's source.txt footage) to a
    video path.
  - ``_probe_seconds(path, ffprobe_cmd, env)`` (L2929-2936) — ffprobe
    the duration in seconds (0.0 on any error).

  The JSON loader and the ffprobe-exe resolver that were local to this
  module are now shared helpers: ``read_json`` + ``ffprobe`` from
  ``services.helpers.common`` (extracted in B11's commit 2, their 3rd
  consumer).

URL CHOICE — SAME URLS AS LEGACY
  All five routes keep their original URLs. There is no ``/api/run``
  dispatcher here (unlike B7/B8), so no URL-namespace split is needed
  — the clone URLs are already self-documenting. The legacy server.py
  keeps all five unchanged; both apps run side by side.

LLM NOTE — INLINE ONE-SHOT CLAUDE CALL (not the ClaudeRunner service)
  ``api_clone_script`` makes a SYNCHRONOUS, blocking Claude call
  (``subprocess.run([claude, "-p", ...], input=prompt, timeout=240)``)
  and returns the text in the HTTP response. This is a faithful move of
  what server.py does — the legacy code has NO shared runner for
  one-shot LLM calls; it repeats this inline pattern across
  /api/clone/script, /api/copywrite, the caption aifix, qc/ai-review,
  and build-vsl. The ``ClaudeRunner`` service (S4) only ever
  centralized the STREAMING, stateful chat path (/api/chat →
  run_chat_turn). Extracting a shared one-shot ``run_oneshot`` method
  is deferred to B12, when the other one-shot callers land on the new
  side and can all be refactored together (no speculative abstraction
  around a single caller — Rule 5.3, no drive-by refactors).

DUB CUTOVER — run_dub_job → dub_worker (finishes the B7 chain)
  Legacy ``api_clone_run`` spawns the dub via
  ``run_dub_job(job_id, cmd, cost_ctx)`` (server.py L3117). This module
  spawns ``dub_worker(job_id, cmd, cost_ctx, runner, ledger)`` — the
  B7b function in routes/dubbing.py — with the same job dict shape and
  the same cost_ctx. This is the call site the B7c cutover was waiting
  for (per the phase-2 recap). ``runner`` + ``ledger`` come from
  app.config (Rule 8.5: passed to the worker thread as explicit args).

DEPENDENCIES:
  - services.jobs:        jobs dict + jobs_lock (busy-check + list state)
  - services.workdir:     DubWorkdir (every per-stem workdir path)
  - services.prompts:     CLONE_PROMPT (the copy prompt, moved in S2)
  - services.llm:         resolve_claude_exe (stateless CLI resolver)
  - services.spend:       SpendLedger (passed to dub_worker via app.config)
  - services.job_runner:  JobRunner (passed to dub_worker via app.config)
  - routes.dubbing:       dub_worker (the shared dub thread target)

LIFECYCLE (per Rule 8.1):
  - Every route resolves its paths/config in the REQUEST context:
    ``AUTOVSL_ROOT``, ``UPLOADS`` (app.py), ``FFMPEG_BIN`` (stashed by
    register_subtitles), ``CV_VENV_PY`` (register_dubbing),
    ``ENGINES_DIR`` (app.py), ``CLAUDE_EXE`` (register_clone),
    ``JOB_RUNNER`` + ``SPEND_LEDGER`` (app.py). None of these routes
    spawn a worker that reads current_app — the only thread spawned is
    ``dub_worker``, which already receives its deps as explicit args.
  - ``register_clone(app)`` is the wire-up point. It resolves and
    stashes ``CLAUDE_EXE`` (mirrors server.py's module-level constant)
    and registers the Blueprint. It must be registered AFTER
    register_dubbing (CV_VENV_PY) and register_subtitles (FFMPEG_BIN).

server.py is unchanged. The clone blocks stay at their original lines
until the B10c cutover lands. Rule 16.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from routes.dubbing import dub_worker
from services.helpers.common import ffprobe, read_json
from services.jobs import jobs, jobs_lock
from services.llm import resolve_claude_exe
from services.prompts import CLONE_PROMPT
from services.workdir import DubWorkdir


# ---------------------------------------------------------------- module helpers

def _probe_seconds(path: Path, ffprobe_cmd: str, env: dict) -> float:
    """Duration of ``path`` in seconds via ffprobe (0.0 on any error).

    Byte-faithful move of server.py L2929-2936. The legacy code used
    ``ff_tool("ffprobe")`` + ``env=job_env()``; here the resolved
    ffprobe command (from ``common.ffprobe``) and the env dict are
    passed in by the route (which reads them from app.config in the
    request context).
    """
    try:
        r = subprocess.run(
            [ffprobe_cmd, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, env=env, timeout=30)
        return float((r.stdout or "0").strip() or 0)
    except (ValueError, subprocess.TimeoutExpired):
        return 0.0


def _winner_script(wd: DubWorkdir) -> str:
    """First non-empty of script-edited.txt / transcript.txt, or "".

    Move of server.py L2939-2946. Now reads the two candidate files
    through the workdir's path properties instead of ``work / name``.
    """
    for p in (wd.script_edited, wd.transcript):
        if p.is_file():
            t = p.read_text(encoding="utf-8", errors="replace").strip()
            if t:
                return t
    return ""


def _clone_actor_video(wd: DubWorkdir, actor: str, uploads: Path) -> Path:
    """Resolve the actor choice to a video path.

    'same' → the winner's original footage (from source.txt); anything
    else → a named video in uploads/. Move of server.py L2990-3003;
    aborts 400 with the same messages on the same failure conditions.
    """
    if actor == "same":
        if not wd.source.is_file():
            abort(400, "this winner has no source.txt — pick an actor video instead")
        video = Path(wd.source.read_text(encoding="utf-8").strip())
        if not video.is_file():
            abort(400, f"the winner's original footage is missing: {video.name}")
        return video
    video = uploads / Path(actor).name
    if not video.is_file():
        abort(400, f"actor video not found in uploads: {actor}")
    return video


# ---------------------------------------------------------------- HTTP routes

clone_bp = Blueprint("clone", __name__)


# -- 1. GET /api/clone/winners -------------------------------------------

@clone_bp.get("/api/clone/winners")
def api_clone_winners():
    """Finished dubs that can serve as the winning template
    (have a final.mp4 + a script).

    Pure relocation of server.py L2949-2973. Enumerates the
    script-swap root (like B4 library / B5 exports) and reads each
    stem's files through DubWorkdir.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    swap_work = autovsl / "output" / "script-swap"
    out = []
    if swap_work.is_dir():
        for d in swap_work.iterdir():
            if not d.is_dir():
                continue
            wd = DubWorkdir(autovsl, d.name)
            final = wd.final
            if not final.is_file():
                continue
            script = _winner_script(wd)
            if not script:
                continue
            source = wd.source.read_text(encoding="utf-8").strip() if wd.source.is_file() else ""
            info = read_json(wd.clone_info) or {}
            out.append({
                "stem": d.name, "mtime": final.stat().st_mtime,
                "script": script, "words": len(script.split()),
                "source": Path(source).name if source else None,
                "has_source": bool(source) and Path(source).is_file(),
                "has_voice": wd.voice.is_file(),
                "is_clone": bool(info), "cloned_from": info.get("winner"),
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"winners": out})


# -- 2. GET /api/clone/actors --------------------------------------------

@clone_bp.get("/api/clone/actors")
def api_clone_actors():
    """Actor footage choices: every video in the uploads library.

    Pure relocation of server.py L2976-2987.
    """
    uploads = current_app.config["UPLOADS"]
    exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    vids = []
    if uploads.is_dir():
        for p in uploads.iterdir():
            if p.is_file() and p.suffix.lower() in exts and p.stat().st_size > 0:
                vids.append({"name": p.name, "size": p.stat().st_size,
                             "mtime": p.stat().st_mtime})
    vids.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"actors": vids})


# -- 3. POST /api/clone/script (inline one-shot Claude call) -------------

@clone_bp.post("/api/clone/script")
def api_clone_script():
    """Generate a similar (winning-pattern) script sized to the chosen
    actor footage.

    Pure relocation of server.py L3006-3051. The Claude call is
    synchronous + inline (see the LLM note in the module docstring) —
    NOT the ClaudeRunner service. The env/CLAUDECODE-pop + CLI flags +
    240s timeout are byte-faithful to the legacy code.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    winner = Path(body.get("winner") or "").name
    wd = DubWorkdir(autovsl, winner)
    if not winner or not wd.dir.is_dir():
        abort(404, "no such winner")
    text = _winner_script(wd)
    if not text:
        abort(400, "the winner has no script/transcript to clone")
    claude_exe = current_app.config["CLAUDE_EXE"]
    if not claude_exe:
        abort(500, "claude CLI not found — install Claude Code or add it to PATH")

    uploads = current_app.config["UPLOADS"]
    video = _clone_actor_video(wd, body.get("actor") or "same", uploads)
    ffprobe_cmd = ffprobe(current_app.config["FFMPEG_BIN"])
    env_factory = current_app.config["JOB_RUNNER"]._job_env_factory
    secs = _probe_seconds(video, ffprobe_cmd, env_factory())
    win_secs = _probe_seconds(wd.final, ffprobe_cmd, env_factory())
    rate = (len(text.split()) / win_secs) if win_secs > 2 else 2.5
    rate = rate if 1.0 <= rate <= 5.0 else 2.5
    if secs > 2:
        target = max(8, round(secs * rate))
        lo, hi = round(target * 0.9), round(target * 1.05)
        length_rule = (f"the footage is {secs:.0f}s and the delivery pace is ~{rate:.1f} "
                       f"words/sec — write {lo}-{hi} words (target ~{target}).")
    else:
        n = len(text.split())
        length_rule = f"match the original length: {round(n*0.9)}-{round(n*1.05)} words."

    steer = (body.get("steer") or "").strip()
    steer_block = f"\nEXTRA DIRECTION FROM THE MARKETER: {steer}\n" if steer else ""
    prompt = CLONE_PROMPT.format(length_rule=length_rule, steer_block=steer_block, text=text)
    env = env_factory()
    env.pop("CLAUDECODE", None)
    try:
        result = subprocess.run(
            [claude_exe, "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch"],
            input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=240, cwd=str(autovsl), env=env)
    except subprocess.TimeoutExpired:
        abort(504, "Claude took too long — try again")
    out = (result.stdout or "").strip()
    if result.returncode != 0 or not out:
        abort(502, f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
    return jsonify({"script": out, "words": len(out.split()),
                    "seconds": round(secs, 1), "rate": round(rate, 2)})


# -- 4. POST /api/clone/run (create workdir + run the dub chain) ---------

@clone_bp.post("/api/clone/run")
def api_clone_run():
    """Create the clone workdir and run the full dub chain on it
    (same GPU/cost gates as Dubbing).

    Pure relocation of server.py L3054-3118, with the B7c cutover:
    the dub is spawned via ``dub_worker`` (routes.dubbing) instead of
    the legacy ``run_dub_job``. Same job dict shape, same cost_ctx.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    winner = Path(body.get("winner") or "").name
    wd = DubWorkdir(autovsl, winner)
    if not winner or not wd.dir.is_dir():
        abort(404, "no such winner")
    script = (body.get("script") or "").strip()
    if len(script.split()) < 8:
        abort(400, "the clone script is empty/too short — generate or paste one first")
    actor = body.get("actor") or "same"
    uploads = current_app.config["UPLOADS"]
    video = _clone_actor_video(wd, actor, uploads)

    with jobs_lock:
        busy = next((j for j in jobs.values()
                     if j["action"] == "dub" and j["status"] == "running"), None)
    if busy:
        abort(409, f"a dub is already running ({busy['slug']}) — the GPU handles one at a time")

    n = 2
    while DubWorkdir(autovsl, f"{winner}-v{n}").dir.exists():
        n += 1
    stem = f"{winner}-v{n}"
    clone_wd = DubWorkdir(autovsl, stem)
    clone_wd.dir.mkdir(parents=True)
    clone_wd.script_edited.write_text(script + "\n", encoding="utf-8")
    clone_wd.source.write_text(str(video) + "\n", encoding="utf-8")
    clone_wd.clone_info.write_text(json.dumps({
        "winner": winner, "actor": "same" if actor == "same" else video.name,
        "created": time.time()}, indent=1), encoding="utf-8")

    engine = body.get("engine") if body.get("engine") in ("local", "fal") else "local"
    venv_py = Path(current_app.config["CV_VENV_PY"])
    engines_dir = current_app.config["ENGINES_DIR"]
    if engine == "local":
        lipsync = body.get("lipsync") if body.get("lipsync") in (
            "none", "wav2lip", "wav2lip-hd") else "wav2lip-hd"
        cmd = [str(venv_py), str(engines_dir / "local_dub.py"), str(video),
               "--name", stem, "--lipsync", lipsync]
        label = f"Clone winner — {stem} (local XTTS + {lipsync}, FREE)"
        cost_ctx = {"engine": "local", "tts": "local", "tier": lipsync,
                    "video": str(video), "stem": stem, "paid": False}
    else:
        if not body.get("confirm_cost"):
            shutil.rmtree(clone_wd.dir, ignore_errors=True)
            abort(400, "FAL.AI clone spends money (voice + TTS + lip-sync) — needs cost approval (confirm_cost)")
        # same actor → reuse the winner's paid voice clone (skips the $1.50 clone fee)
        if actor == "same" and wd.voice.is_file():
            shutil.copy2(wd.voice, clone_wd.voice)
        tier = body.get("tier") if body.get("tier") in ("pro", "standard", "veed", "latentsync") else "standard"
        tts = body.get("tts") if body.get("tts") in ("hd", "turbo", "f5") else "hd"
        cmd = [str(venv_py), str(engines_dir / "dub.py"), str(video),
               "--name", stem, "--tier", tier, "--tts", tts]
        label = f"Clone winner — {stem} (fal {tts}/{tier}) $"
        cost_ctx = {"engine": "fal", "tts": tts, "tier": tier,
                    "video": str(video), "stem": stem, "paid": True}

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "action": "dub", "slug": stem, "label": label,
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
    }
    runner = current_app.config["JOB_RUNNER"]
    ledger = current_app.config["SPEND_LEDGER"]
    threading.Thread(
        target=dub_worker, args=(job_id, cmd, cost_ctx, runner, ledger),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id, "stem": stem})


# -- 5. GET /api/clone/list ----------------------------------------------

@clone_bp.get("/api/clone/list")
def api_clone_list():
    """All clones made so far, with their state (running / ready / failed).

    Pure relocation of server.py L3121-3143.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    swap_work = autovsl / "output" / "script-swap"
    clones = []
    if swap_work.is_dir():
        for d in swap_work.iterdir():
            if not d.is_dir():
                continue
            wd = DubWorkdir(autovsl, d.name)
            info = read_json(wd.clone_info)
            if not info:
                continue
            final = wd.final
            with jobs_lock:
                cand = [j for j in jobs.values()
                        if j["slug"] == d.name and j["action"] == "dub"]
                job = max(cand, key=lambda j: j["started"]) if cand else None
            clones.append({
                "stem": d.name, "winner": info.get("winner"), "actor": info.get("actor"),
                "created": info.get("created"),
                "ready": final.is_file() and final.stat().st_size > 0,
                "final_mtime": final.stat().st_mtime if final.is_file() else None,
                "job": {"id": job["id"], "status": job["status"]} if job else None,
            })
    clones.sort(key=lambda x: x.get("created") or 0, reverse=True)
    return jsonify({"clones": clones})


# ---------------------------------------------------------------- wire-up

def register_clone(app) -> None:
    """Register the Clone Winner Blueprint and stash ``CLAUDE_EXE``.

    ``CLAUDE_EXE`` is resolved once here (mirrors server.py's
    module-level constant at L22-25) via the stateless
    ``resolve_claude_exe`` helper in services/llm.py. B12 (chat) will
    construct the ClaudeRunner and can read the same key.

    Must be registered AFTER register_dubbing (sets ``CV_VENV_PY``) and
    register_subtitles (sets ``FFMPEG_BIN``); ``ENGINES_DIR`` +
    ``UPLOADS`` + ``AUTOVSL_ROOT`` come from app.py. No new derivation
    of those keys here — each register_* owns the keys it introduces.
    """
    app.config["CLAUDE_EXE"] = resolve_claude_exe()
    app.register_blueprint(clone_bp)
