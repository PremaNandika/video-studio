"""QC route module — the QA Review tab.

Pure move of the QC cluster from video-studio/app/server.py L1896-2151:

  1. ``GET  /api/qc/videos``      (L1896-1926) — list every QC-able video
     (dubs, VSLs, nosubs, sources) + the saved reviews.
  2. ``GET  /api/qc/models``      (L1929-1963) — scoreboard: how each
     voice+lipsync combo scores across reviews.
  3. ``GET  /api/qc/probe``       (L1966-1969) — ffprobe JSON of one video.
  4. ``GET  /api/qc/frames``      (L1972-1976) — N evenly-spaced frames.
  5. ``POST /api/qc/review``      (L1979-1994) — save a manual verdict.
  6. ``POST /api/qc/ai-review``   (L2065-2076) — job: Claude-vision review.
  7. ``POST /api/qc/remove-subs`` (L2079-2151) — strip/erase/delogo/blur/crop.

Plus ``qc_ai_worker`` (L1997-2062), the ai-review thread target.

HELPERS LIVE IN services/helpers/qc.py
  The 8 QC support functions (safe_video_path, ffprobe_json,
  video_duration, qc_cache_dir, extract_spread_frames,
  extract_burst_frames, qc_store, qc_save) + qc_lock are in the helper
  module, so this route module stays thin (7 routes + 1 worker). They
  take a ``cfg`` dict (built by ``_qc_cfg`` in request context) rather
  than current_app, so ``qc_ai_worker`` can call them from its thread.

THE WORKER — B9 TRAP AVOIDED
  ``qc_ai_worker`` runs on a bare daemon thread. It receives the resolved
  ``cfg`` bundle + ``claude_exe`` as explicit args and NEVER touches
  current_app (same rule as clean_subs_worker / dub_worker). The
  ``ai-review`` route resolves everything in request context first.

LLM CALL — inline one-shot (Option 1)
  ``qc_ai_worker`` makes a synchronous Claude-vision call (opus, Read
  tool) with ``QC_PROMPT`` from services.prompts — faithful to the
  monolith, no shared runner (see the run_oneshot deferral note).
  ``claude_exe`` is read from CLAUDE_RUNNER (B12), not register_clone.

remove-subs spawns the engine via ``runner.run`` (all 5 methods); the
erase method is a GPU job (erase_subs.py) with the legacy 409 guard.

DEPENDENCIES:
  - services.helpers.qc:     the 8 helpers + qc_lock + QC_VIDEO_EXTS
  - services.helpers.common: ffmpeg + ffprobe (command resolvers)
  - services.jobs:           jobs + jobs_lock (ai-review + remove-subs jobs)
  - services.prompts:        QC_PROMPT (the vision-review prompt)
  - app.config:              AUTOVSL_ROOT, FFMPEG_BIN, UPLOADS, JOB_RUNNER,
                             CLAUDE_RUNNER, CV_VENV_PY (erase), ERASE_PY (erase)

server.py is unchanged. The QC blocks stay at their original lines until
the B14c cutover lands. Rule 16.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.helpers.common import ffmpeg, ffprobe, read_json, safe_video_path
from services.helpers.qc import (
    QC_VIDEO_EXTS, extract_burst_frames, extract_spread_frames, ffprobe_json,
    qc_lock, qc_save, qc_store, video_duration,
)
from services.jobs import jobs, jobs_lock
from services.prompts import QC_PROMPT


# ---------------------------------------------------------------- cfg builder

def _qc_cfg() -> dict:
    """Build the config bundle the QC helpers + worker need.

    Called in REQUEST context (has current_app). The resolved dict is
    safe to hand to ``qc_ai_worker`` on its thread — it contains only
    plain paths / strings / an env dict, no Flask objects.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    ffbin = current_app.config["FFMPEG_BIN"]
    return {
        "autovsl_root": autovsl,
        "qc_cache": autovsl / "output" / "qc" / "cache",
        "qc_reviews": autovsl / "output" / "qc" / "reviews.json",
        "ffmpeg": ffmpeg(ffbin),
        "ffprobe": ffprobe(ffbin),
        "env": current_app.config["JOB_RUNNER"]._job_env_factory(),
    }


# ---------------------------------------------------------------- HTTP routes

qc_bp = Blueprint("qc", __name__)


@qc_bp.get("/api/qc/videos")
def api_qc_videos():
    """List every QC-able video (dubs / VSLs / nosubs / sources) + reviews.
    Pure relocation of server.py L1896-1926."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    uploads = current_app.config["UPLOADS"]
    swap_work = autovsl / "output" / "script-swap"
    nosubs_dir = autovsl / "output" / "nosubs"
    vids = []

    def add(kind, group, p, models=""):
        vids.append({"kind": kind, "group": group, "name": p.name,
                     "path": str(p.relative_to(autovsl)).replace("\\", "/"),
                     "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                     "models": models})

    if swap_work.is_dir():
        for d in sorted(swap_work.iterdir()):
            if d.is_dir():
                cfg = read_json(d / "dub-config.json") or {}
                models = " · ".join(x for x in (
                    ("voice: " + cfg["tts"]) if cfg.get("tts") else "",
                    ("lips: " + cfg["tier"]) if cfg.get("tier") else "",
                    ("engine: " + cfg["engine"]) if cfg.get("engine") else "") if x)
                for f in sorted(d.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                    add("dub", d.name, f, models if f.name == "final.mp4" else "")
    for p in sorted((autovsl / "output").glob("*.mp4")):
        add("vsl", None, p)
    if nosubs_dir.is_dir():
        for p in sorted(nosubs_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            add("nosubs", None, p)
    if uploads.is_dir():
        for p in sorted(uploads.iterdir()):
            if p.is_file() and p.suffix.lower() in QC_VIDEO_EXTS:
                add("source", None, p)

    return jsonify({"videos": vids, "reviews": qc_store(_qc_cfg())})


@qc_bp.get("/api/qc/models")
def api_qc_models():
    """Scoreboard: how each voice+lip-sync combo performs, from QC reviews.
    Pure relocation of server.py L1929-1963."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    swap_work = autovsl / "output" / "script-swap"
    reviews = qc_store(_qc_cfg())
    combos: dict[str, dict] = {}
    if swap_work.is_dir():
        for d in swap_work.iterdir():
            if not d.is_dir():
                continue
            cfg = read_json(d / "dub-config.json") or {}
            if not cfg.get("tts"):
                continue
            combo = f"{cfg.get('tts')} + {cfg.get('tier', '?')}"
            rel = f"output/script-swap/{d.name}/final.mp4"
            r = reviews.get(rel) or {}
            c = combos.setdefault(combo, {"combo": combo, "videos": 0, "reviewed": 0,
                                          "passes": 0, "fails": 0, "scores": []})
            c["videos"] += 1
            ai = r.get("ai") or {}
            sc = [(ai.get(k) or {}).get("score") for k in ("mouth", "realism", "quality")]
            sc = [s for s in sc if isinstance(s, (int, float))]
            if sc:
                c["reviewed"] += 1
                c["scores"].append(sum(sc) / len(sc))
            if r.get("verdict") == "pass":
                c["passes"] += 1
            elif r.get("verdict") == "fail":
                c["fails"] += 1
    out = []
    for c in combos.values():
        avg = round(sum(c["scores"]) / len(c["scores"]), 1) if c["scores"] else None
        out.append({"combo": c["combo"], "videos": c["videos"], "reviewed": c["reviewed"],
                    "avg_score": avg, "passes": c["passes"], "fails": c["fails"]})
    out.sort(key=lambda x: (-(x["avg_score"] or 0), -x["videos"]))
    return jsonify(out)


@qc_bp.get("/api/qc/probe")
def api_qc_probe():
    """ffprobe JSON of one video. Pure relocation of server.py L1966-1969."""
    src = safe_video_path(request.args.get("path", ""), current_app.config["AUTOVSL_ROOT"])
    return jsonify(ffprobe_json(src, _qc_cfg()))


@qc_bp.get("/api/qc/frames")
def api_qc_frames():
    """N evenly-spaced frames for review. Pure relocation of server.py L1972-1976."""
    src = safe_video_path(request.args.get("path", ""), current_app.config["AUTOVSL_ROOT"])
    count = min(max(int(request.args.get("count", 10)), 4), 24)
    return jsonify({"frames": extract_spread_frames(src, count, _qc_cfg())})


@qc_bp.post("/api/qc/review")
def api_qc_review():
    """Save a manual QC verdict/checks/notes. Pure relocation of server.py L1979-1994."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    src = safe_video_path(body.get("path", ""), autovsl)
    rel = str(src.relative_to(autovsl)).replace("\\", "/")
    cfg = _qc_cfg()
    with qc_lock:
        store = qc_store(cfg)
        entry = store.get(rel) or {}
        entry["checks"] = {k: v for k, v in (body.get("checks") or {}).items()
                           if k in ("lip_sync", "dubbing", "quality", "realism") and v in ("pass", "fail", "na")}
        entry["notes"] = (body.get("notes") or "")[:2000]
        entry["verdict"] = body.get("verdict") if body.get("verdict") in ("pass", "fail", "pending") else "pending"
        entry["updated"] = time.time()
        store[rel] = entry
        qc_save(store, cfg)
    return jsonify({"saved": rel})


@qc_bp.post("/api/qc/ai-review")
def api_qc_ai_review():
    """Spawn a Claude-vision QC review job. Pure relocation of server.py L2065-2076.

    Resolves the cfg + claude_exe in request context and hands them to
    ``qc_ai_worker`` (which runs on a thread with no app context).
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    src = safe_video_path(request.get_json(force=True).get("path", ""), autovsl)
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "claude CLI not found")
    rel = str(src.relative_to(autovsl)).replace("\\", "/")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "qc-ai", "slug": src.stem,
                    "label": f"AI QC review — {src.name}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    threading.Thread(target=qc_ai_worker, args=(job_id, rel, _qc_cfg(), runner.claude_exe),
                     daemon=True).start()
    return jsonify({"job_id": job_id})


@qc_bp.post("/api/qc/remove-subs")
def api_qc_remove_subs():
    """Clear subtitles: strip embedded tracks, remove/blur a burned-in region, or crop.
    Pure relocation of server.py L2079-2151."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    src = safe_video_path(body.get("path", ""), autovsl)
    method = body.get("method")
    if method not in ("strip", "erase", "delogo", "blur", "crop"):
        abort(400, "method must be strip | erase | delogo | blur | crop")

    nosubs_dir = autovsl / "output" / "nosubs"
    nosubs_dir.mkdir(parents=True, exist_ok=True)
    out = nosubs_dir / f"{src.stem}-{method}.mp4"
    if out.exists():
        out = nosubs_dir / f"{src.stem}-{method}-{time.strftime('%H%M%S')}.mp4"

    cfg = _qc_cfg()
    ffmpeg_cmd = cfg["ffmpeg"]
    if method == "strip":
        cmd = [ffmpeg_cmd, "-y", "-i", str(src), "-map", "0", "-map", "-0:s?",
               "-map", "-0:d?", "-c", "copy", str(out)]
    elif method == "erase":
        # per-frame text detection + inpaint of only the letter pixels (erase_subs.py);
        # without a box the script auto-detects where the captions sit
        with jobs_lock:
            if any(j["action"] == "remove-subs" and j["status"] == "running" for j in jobs.values()):
                abort(409, "another subtitle-removal job is already running — the GPU can only handle "
                           "one at a time; wait for it to finish")
        venv_py = Path(current_app.config["CV_VENV_PY"])
        cmd = [str(venv_py), str(current_app.config["ERASE_PY"]), str(src), str(out)]
        coords = [body.get(k) for k in ("x", "y", "w", "h")]
        if all(c is not None for c in coords):
            try:
                x, y, w, h = (int(c) for c in coords)
            except (TypeError, ValueError):
                abort(400, "x/y/w/h must be integers")
            if w < 8 or h < 8:
                abort(400, "search box too small — draw a bigger box, or use the automatic button")
            cmd += ["--x", str(x), "--y", str(y), "--w", str(w), "--h", str(h)]
    else:
        probe = ffprobe_json(src, cfg)
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        W, H = int(v.get("width") or 0), int(v.get("height") or 0)
        if not W or not H:
            abort(400, "could not read video dimensions")
        if method == "crop":
            bottom = int(body.get("bottom") or 0)
            if not (0 < bottom < H - 16):
                abort(400, f"bottom must be between 1 and {H - 16}")
            vf = f"crop=iw:ih-{bottom}:0:0"
        else:
            try:
                x, y, w, h = (int(body.get(k) or 0) for k in ("x", "y", "w", "h"))
            except (TypeError, ValueError):
                abort(400, "x/y/w/h must be integers")
            # clamp: delogo requires the box strictly inside the frame
            x = min(max(1, x), W - 12)
            y = min(max(1, y), H - 12)
            w = min(max(8, w), W - x - 2)
            h = min(max(8, h), H - y - 2)
            if method == "delogo":
                vf = f"delogo=x={x}:y={y}:w={w}:h={h}"
            else:
                r = max(2, min(w, h) // 4)
                vf = f"[0:v]crop={w}:{h}:{x}:{y},boxblur={r}:2[b];[0:v][b]overlay={x}:{y}"
        filt = ["-filter_complex", vf] if method == "blur" else ["-vf", vf]
        cmd = [ffmpeg_cmd, "-y", "-i", str(src), *filt,
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-c:a", "copy", "-movflags", "+faststart", str(out)]

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "remove-subs", "slug": src.stem,
                    "label": f"Clear subtitles ({method}) — {src.name}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(autovsl)).replace("\\", "/")})


# ---------------------------------------------------------------- worker thread

def qc_ai_worker(job_id: str, rel: str, cfg: dict, claude_exe: str) -> None:
    """Claude-vision QC review on a daemon thread.

    Pure relocation of server.py L1997-2062. Receives the resolved
    ``cfg`` bundle + ``claude_exe`` as explicit args — NEVER touches
    current_app (bare thread, no app context; the B9 rule). Extracts
    frames, asks Claude to grade them (QC_PROMPT), and writes the review
    into the reviews store under ``qc_lock``.
    """
    job = jobs[job_id]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        src = cfg["autovsl_root"] / rel
        probe = ffprobe_json(src, cfg)
        dur = video_duration(probe)
        v = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        kbps = int(probe.get("format", {}).get("bit_rate") or 0) // 1000
        specs = (f"{v.get('width', '?')}x{v.get('height', '?')} · {v.get('r_frame_rate', '?')} fps · "
                 f"{dur:.1f}s · ~{kbps} kb/s total · codec {v.get('codec_name', '?')}")
        log(f"Probing done: {specs}")

        log("Extracting 6 spread frames + 6 burst frames (mid-speech)...")
        spread = extract_spread_frames(src, 6, cfg)
        burst = extract_burst_frames(src, max(0.0, dur * 0.4), cfg)
        if not spread:
            raise RuntimeError("could not extract frames (ffmpeg failed or zero duration)")

        prompt = QC_PROMPT.format(
            rel=rel, specs=specs,
            spread="\n".join(f"- {f['path']}  (t={f['t']}s)" for f in spread),
            burst="\n".join(f"- {p}" for p in burst) or "(none — video too short)",
        )
        log("Asking Claude to review the frames (takes a minute or two)...")
        cenv = dict(cfg["env"])
        cenv.pop("CLAUDECODE", None)
        result = subprocess.run(
            [claude_exe, "-p", "--model", "opus", "--allowedTools", "Read",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
            input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, cwd=str(cfg["autovsl_root"]), env=cenv,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out:
            raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
        if out.startswith("```"):
            out = out.split("```")[1].lstrip("json").strip()
        start, end = out.find("{"), out.rfind("}")
        review = json.loads(out[start:end + 1].replace("�", "-"))

        review["reviewed"] = time.time()
        review["frames"] = [f["path"] for f in spread] + burst
        with qc_lock:
            store = qc_store(cfg)
            entry = store.get(rel) or {}
            entry["ai"] = review
            store[rel] = entry
            qc_save(store, cfg)

        o = review.get("overall") or {}
        log(f"Verdict: {str(o.get('verdict', '?')).upper()} — {o.get('summary', '')}")
        for k in ("mouth", "realism", "quality"):
            log(f"  {k}: {(review.get(k) or {}).get('score', '?')}/10")
        job["returncode"] = 0
        job["status"] = "done"
    except Exception as exc:
        log(f"AI REVIEW FAILED: {exc}")
        job["returncode"] = 1
        job["status"] = "failed"
    finally:
        job["ended"] = time.time()


# ---------------------------------------------------------------- wire-up

def register_qc(app) -> None:
    """Register the QC Blueprint. Stashes no config keys of its own — every
    path derives from AUTOVSL_ROOT / FFMPEG_BIN (app.py), and the erase
    path uses CV_VENV_PY (register_dubbing) + ERASE_PY (register_subtitles),
    so register_qc must run after those. CLAUDE_RUNNER (B12) + JOB_RUNNER
    (app.py) supply the LLM exe + the engine runner."""
    app.register_blueprint(qc_bp)
