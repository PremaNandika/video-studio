"""DubSync route module — the repair suite + take promotion.

B11 lands this module in two commits (per the agreed plan):

  * Commit 3 (this file, repair side):
      1. ``POST /api/dubsync/repair``        (server.py L2428-2510)
      2. ``POST /api/dubsync/visual-preview`` (server.py L2513-2553)
      3. ``POST /api/dub-promote``            (server.py L1576-1608)
      4. ``GET  /api/dubs``                   (server.py L2395-2425)
  * Commit 4 (advise/upload side, appended here):
      5. ``POST /api/dubsync/advise`` (server.py L2625-2764) — the
         no-drawing flow: inline Claude-vision one-shot (sonnet, Read
         tool) reads sample frames, picks the repair + locates boxes,
         returns a ready-to-run config + preview strip.
      6. ``POST /api/dubsync/upload`` (server.py L2845-2887) — drop the
         damaged video; the ZNCC content-fingerprint auto-matcher
         (``_find_original`` → ``_video_meta`` + ``_sparse_thumbs``)
         finds its original in the uploads library.

  The advise Claude call is inline (Option 1, like B10's clone/script) —
  the monolith has no shared runner for one-shot LLM calls; ADVISE_PROMPT
  comes from services.prompts (moved in S2). The ZNCC trio is used ONLY
  by upload, so nothing straddles the repair/advise-upload commit split.

URL CHOICE — SAME URLS AS LEGACY
  Every route keeps its original URL. No ``/api/run`` dispatcher is
  involved, so no namespace split (unlike B7/B8). Both apps run side
  by side; the frontend picks which to hit.

THE REPAIR ENGINES (moved in E1 to backend-app/engines/)
  ``/api/dubsync/repair`` and ``/api/dubsync/visual-preview`` invoke the
  repair engine scripts as subprocesses under the CV venv:
    - frame_swap.py     (swap: replace marked ranges 1:1 from original)
    - object_repair.py  (object: keep dub, restore a damaged object)
    - visual_repair.py  (visual: keep original, restore the dub's lips)
    - dubsync_repair.py (remux / refit / renorm / relipsync)
  These are BYTE-IDENTICAL to the monolith's engines (Rule 10). They
  read no env vars; they call ffmpeg/ffprobe by bare name (the job env
  provides both on PATH) and — for ``relipsync`` — receive the
  dubbing-studio venv + lipsync.py path as ``--dub-python`` /
  ``--lipsync`` args (stashed by ``register_dubsync``).

  frame_swap.py + object_repair.py do a flat ``from visual_repair import
  ...``; that resolves because the engine's own directory is on
  sys.path[0] when run as ``python <engines>/frame_swap.py`` and all
  engines are co-located. Do not split them across directories.

GPU LOCK — ONLY relipsync (faithful to legacy)
  The repair job sets ``job["gpu"] = (action == "relipsync")``. The
  repair-engine script names are NOT in ``GPU_MARKERS``, so the runner
  would otherwise not lock the GPU for them — matching the monolith,
  which only ever GPU-gated relipsync (Wav2Lip). The explicit flag
  preserves that exactly.

DUB PROMOTE — uses DubWorkdir.promote() (B11 commit 1)
  ``/api/dub-promote`` validates the take (HTTP concern, aborts 404),
  calls ``DubWorkdir.promote(fname)`` for all workdir-internal state
  (archive current final, rename take → final, update versions.json /
  dub-config.json), then does the external READY_DIR deliverable copy
  (outside the workdir, so it stays in the route per Rule 8.3).

DEPENDENCIES:
  - services.jobs:           jobs dict (repair job record)
  - services.workdir:        DubWorkdir (paths + promote())
  - services.helpers.common: read_json (versions.json in /api/dubs)
  - services.job_runner:     JobRunner (repair engine subprocess +
                             the env factory for the sync preview)

LIFECYCLE (per Rule 8.1):
  - Every route resolves config in the REQUEST context: ``AUTOVSL_ROOT``
    (app.py), ``CV_VENV_PY`` (register_dubbing), ``REPAIR_ENGINES_DIR`` +
    ``DUB_VENV_PY`` + ``LIPSYNC_PY`` (register_dubsync), ``READY_DIR``
    (app.py), ``JOB_RUNNER`` (app.py).
  - The only thread spawned is ``runner.run`` (repair) with args baked
    in the request handler — no worker reads current_app (the B9 trap
    cannot recur). visual-preview / dub-promote / dubs are synchronous.
  - ``register_dubsync(app)`` is the wire-up point. Must register AFTER
    register_dubbing (CV_VENV_PY). It stashes REPAIR_ENGINES_DIR (the
    backend-app/engines/ dir), DUB_VENV_PY, LIPSYNC_PY.

server.py is unchanged. The dubsync/promote/dubs blocks stay at their
original lines until the B11c cutover lands. Rule 16.
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
from werkzeug.utils import secure_filename

from services.helpers.common import ffprobe, read_json
from services.jobs import jobs
from services.prompts import ADVISE_PROMPT
from services.workdir import DubWorkdir


# ---------------------------------------------------------------- constants

# Fractions of the video duration at which advise samples frames for the
# vision call (server.py L2593). 7 evenly-ish spaced points.
ADVISE_FRACS = (0.08, 0.22, 0.36, 0.50, 0.64, 0.78, 0.92)

_REPAIR_ACTIONS = ("remux", "refit", "renorm", "relipsync", "visual", "object", "swap")
_REPAIR_LABELS = {
    "remux": "Re-mux voice", "refit": "Re-fit voice length",
    "renorm": "Re-normalize loudness", "relipsync": "Re-run lip-sync",
    "visual": "Fix visuals from original",
    "object": "Fix damaged object (keep the dub)",
    "swap": "Swap marked moments with original",
}


# ---------------------------------------------------------------- HTTP routes

dubsync_bp = Blueprint("dubsync", __name__)


# -- 1. POST /api/dubsync/repair -----------------------------------------

@dubsync_bp.post("/api/dubsync/repair")
def api_dubsync_repair():
    """Fix a finished dub without re-dubbing (swap | object | visual |
    remux | refit | renorm | relipsync). Output is a new versioned take;
    final.mp4 is untouched until the user promotes.

    Pure relocation of server.py L2428-2510. Engine paths now come from
    ``REPAIR_ENGINES_DIR`` (backend-app/engines/); the job is spawned
    via ``runner.run`` (JobRunner) instead of the monolith's ``run_job``.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    stem = Path(body.get("stem") or "").name
    action = body.get("action")
    if action not in _REPAIR_ACTIONS:
        abort(400, "bad action")
    wd = DubWorkdir(autovsl, stem)
    work = wd.dir
    if not stem or not wd.final.is_file():
        abort(404, "no such dub")
    venv_py = Path(current_app.config["CV_VENV_PY"])
    eng = current_app.config["REPAIR_ENGINES_DIR"]
    if action == "swap":
        ranges = body.get("ranges") or []
        parts = []
        for rr in ranges:
            try:
                s0, s1 = float(rr["start"]), float(rr["end"])
            except (KeyError, TypeError, ValueError):
                abort(400, "each range needs numeric start/end seconds")
            if s1 <= s0:
                abort(400, f"range {s0:.2f}-{s1:.2f}: end must be after start")
            parts.append(f"{s0:.3f}-{s1:.3f}")
        if not parts:
            abort(400, "mark at least one time range first")
        cmd = [str(venv_py), str(eng / "frame_swap.py"), "--work", str(work),
               "--ranges", ",".join(parts)]
        if body.get("fade"):
            cmd += ["--fade", str(int(body["fade"]))]
    elif action == "object":
        samples = body.get("samples") or []
        if not any(s.get("obj") for s in samples):
            abort(400, "object repair needs at least one object box — describe the object in the chat first")
        rois = work / "object-rois.json"
        rois.write_text(json.dumps({"samples": samples}, indent=1), encoding="utf-8")
        cmd = [str(venv_py), str(eng / "object_repair.py"), "--work", str(work), "--rois", str(rois)]
        if body.get("thresh"):
            cmd += ["--thresh", str(float(body["thresh"]))]
        if body.get("color_fix"):
            cmd.append("--color-fix")
    elif action == "visual":
        box = body.get("box") or {}
        try:
            bx, by, bw, bh = (int(box[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            abort(400, "visual repair needs box {x,y,w,h} over the lips")
        if bw < 16 or bh < 16:
            abort(400, "protected box is too small")
        cmd = [str(venv_py), str(eng / "visual_repair.py"), "--work", str(work),
               "--box", str(bx), str(by), str(bw), str(bh)]
        if not body.get("color_fix", True):
            cmd.append("--no-color-fix")
        if body.get("track"):
            cmd.append("--track")
        if body.get("encoder") == "nvenc":
            cmd += ["--encoder", "nvenc"]
    else:
        cmd = [str(venv_py), str(eng / "dubsync_repair.py"), action, "--work", str(work)]
    if action == "relipsync":
        restorer = body.get("restorer", "gfpgan")
        if restorer not in ("gfpgan", "codeformer", "none"):
            abort(400, "bad restorer")
        dub_venv = Path(current_app.config["DUB_VENV_PY"])
        if not dub_venv.is_file():
            abort(500, f"dubbing-studio venv missing: {dub_venv}")
        cmd += ["--restorer", restorer, "--upscale", str(int(body.get("upscale") or 1)),
                "--fidelity", str(float(body.get("fidelity") or 0.7)),
                "--dub-python", str(dub_venv), "--lipsync", str(current_app.config["LIPSYNC_PY"])]
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "action": "dubsync-repair", "slug": stem,
        "label": f"DubSync Repair ({_REPAIR_LABELS[action]}) — {stem}",
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
        "gpu": action == "relipsync",   # only Wav2Lip needs the GPU
    }
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id})


# -- 2. POST /api/dubsync/visual-preview (synchronous) -------------------

@dubsync_bp.post("/api/dubsync/visual-preview")
def api_dubsync_visual_preview():
    """Synchronous single-frame preview of the visual repair
    (ORIGINAL | DUBBED | REPAIRED | DIFF strip) so the user can check
    box placement + alignment before committing.

    Pure relocation of server.py L2513-2553. Runs visual_repair.py with
    ``--preview-at``; the env comes from the JobRunner's factory (so
    ffmpeg/ffprobe are on PATH), same as B9's clean-preview.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    stem = Path(body.get("stem") or "").name
    wd = DubWorkdir(autovsl, stem)
    if not stem or not wd.final.is_file():
        abort(404, "no such dub")
    box = body.get("box") or {}
    try:
        bx, by, bw, bh = (int(box[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        abort(400, "needs box {x,y,w,h}")
    try:
        at = float(body.get("at") or 1.0)
    except (TypeError, ValueError):
        at = 1.0
    eng = current_app.config["REPAIR_ENGINES_DIR"]
    cmd = [str(Path(current_app.config["CV_VENV_PY"])), str(eng / "visual_repair.py"),
           "--work", str(wd.dir), "--box", str(bx), str(by), str(bw), str(bh),
           "--preview-at", f"{at:.3f}"]
    if not body.get("color_fix", True):
        cmd.append("--no-color-fix")
    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90, env=env)
    except subprocess.TimeoutExpired:
        abort(504, "preview took too long")
    out = (r.stdout or "")
    if r.returncode != 0:
        err = next((ln for ln in out.splitlines() if ln.startswith("ERROR:")), out[-300:])
        abort(500, err)
    align = {}
    for ln in out.splitlines():
        if ln.startswith("ALIGN:"):
            try:
                align = json.loads(ln[6:].strip())
            except json.JSONDecodeError:
                pass
    warn = next((ln for ln in out.splitlines() if ln.startswith("WARNING:")), None)
    return jsonify({"img": f"/media/output/script-swap/{stem}/preview-visual.jpg",
                    "align": align, "warning": warn, "ts": time.time()})


# -- 3. POST /api/dub-promote --------------------------------------------

@dubsync_bp.post("/api/dub-promote")
def api_dub_promote():
    """Crown an archived dub take as the current final (and refresh the
    Desktop deliverable).

    Pure relocation of server.py L1576-1608. The workdir-internal
    promotion is ``DubWorkdir.promote()`` (B11 commit 1); the external
    READY_DIR deliverable copy stays here (outside the workdir).
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    stem = Path(body.get("stem") or "").name
    fname = Path(body.get("file") or "").name
    wd = DubWorkdir(autovsl, stem)
    src = wd.dir / fname
    if (not stem or not fname or fname.startswith(("new-vo", "final-captioned"))
            or src.suffix != ".mp4" or not src.is_file()):
        abort(404, "version not found")
    result = wd.promote(fname)
    if fname != "final.mp4":   # a real promotion happened → refresh the deliverable
        ready = current_app.config["READY_DIR"]
        ready.mkdir(parents=True, exist_ok=True)
        shutil.copy2(wd.final, ready / f"{stem}-ready.mp4")
    return jsonify(result)


# -- 4. GET /api/dubs -----------------------------------------------------

@dubsync_bp.get("/api/dubs")
def api_dubs():
    """Every dub workdir with its provenance + version history.

    Pure relocation of server.py L2395-2425. Enumerates the script-swap
    root (B4/B5 pattern) and reads per-stem files via DubWorkdir; the
    take listing is inline (does not use the monolith's ``dub_versions``
    helper, which has a different consumer and stays in server.py).
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    swap_work = autovsl / "output" / "script-swap"
    dubs = []
    if swap_work.is_dir():
        for d in sorted(swap_work.iterdir()):
            if not d.is_dir():
                continue
            wd = DubWorkdir(autovsl, d.name)
            final = wd.final
            if not final.is_file():
                continue
            source = wd.source.read_text(encoding="utf-8").strip() if wd.source.is_file() else ""
            versions = read_json(wd.versions) or {}
            takes = []
            for p in sorted(wd.dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
                if p.name.startswith(("new-vo", "final-captioned")) or p.name == "final.mp4":
                    continue
                if p.stat().st_size == 0:      # failed/aborted runs leave empty files
                    continue
                takes.append({"file": p.name, "mtime": p.stat().st_mtime,
                              "size": p.stat().st_size,
                              "repair": (versions.get(p.name) or {}).get("repair")})
            dubs.append({
                "stem": d.name,
                "final_mtime": final.stat().st_mtime,
                "final_size": final.stat().st_size,
                "has_vo": wd.new_vo.is_file(),
                "has_source": bool(source) and Path(source).is_file(),
                "takes": takes[:12],
            })
    dubs.sort(key=lambda x: x["final_mtime"], reverse=True)
    return jsonify({"dubs": dubs})


# ================================================================ ADVISE / UPLOAD
# (B11 commit 4 — appended to the same blueprint; no register_dubsync change,
#  advise uses CLAUDE_EXE [register_clone] + FFMPEG_BIN [register_subtitles] +
#  REPAIR_ENGINES_DIR [register_dubsync]; upload uses UPLOADS [app.py].)

# ---------------------------------------------------------------- advise helper

def _advise_frames(work: Path, final: Path, ffprobe_cmd: str, env: dict):
    """Extract sample frames (≤960px tall) for the vision call.
    Returns (paths, frame_indices, iw, ih, scale).

    Byte-faithful move of server.py L2596-2622. The resolved ffprobe
    command + subprocess env are passed in by the route (request
    context); frame extraction shells to bare ``ffmpeg`` (on PATH via
    the job env), exactly as the monolith did.
    """
    pr = subprocess.run([ffprobe_cmd,
                         "-v", "error", "-show_entries", "format=duration",
                         "-select_streams", "v:0",
                         "-show_entries", "stream=width,height,r_frame_rate",
                         "-of", "json", str(final)],
                        capture_output=True, text=True, env=env)
    d = json.loads(pr.stdout or "{}")
    W = int(d["streams"][0]["width"]); H = int(d["streams"][0]["height"])
    num, den = (int(x) for x in d["streams"][0]["r_frame_rate"].split("/"))
    fps = num / max(1, den)
    dur = float(d["format"].get("duration") or 10)
    scale = min(1.0, 960.0 / H)
    iw, ih = int(W * scale) // 2 * 2, int(H * scale) // 2 * 2
    paths, indices = [], []
    for i, frac in enumerate(ADVISE_FRACS):
        t = dur * frac
        p = work / f"advise-{i + 1}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                        "-i", str(final), "-frames:v", "1", "-vf", f"scale={iw}:{ih}", str(p)],
                       capture_output=True, env=env)
        if p.is_file():
            paths.append(p)
            indices.append(int(round(t * fps)))
    return paths, indices, iw, ih, scale


# -- 5. POST /api/dubsync/advise (inline Claude-vision one-shot) ---------

@dubsync_bp.post("/api/dubsync/advise")
def api_dubsync_advise():
    """No-drawing flow: the user describes what's wrong (or nothing);
    local Claude LOOKS at sample frames, locates the lips, picks the
    repair, and the server returns a ready-to-run config + preview strip.

    Pure relocation of server.py L2625-2764. The Claude call is inline
    (Option 1); ADVISE_PROMPT is imported from services.prompts. Preview
    subprocesses use the repair engines under REPAIR_ENGINES_DIR.
    """
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    stem = Path(body.get("stem") or "").name
    wd = DubWorkdir(autovsl, stem)
    work = wd.dir
    final = wd.final
    if not stem or not final.is_file():
        abort(404, "no such dub")
    claude_exe = current_app.config["CLAUDE_EXE"]
    if not claude_exe:
        abort(500, "local Claude CLI not found — describe-and-fix needs it (draw the box instead)")
    complaint = (body.get("text") or "").strip() or "(none — locate the lips, default to visual)"

    ffprobe_cmd = ffprobe(current_app.config["FFMPEG_BIN"])
    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    frames, indices, iw, ih, scale = _advise_frames(work, final, ffprobe_cmd, env)
    if not frames:
        abort(500, "could not extract sample frames")
    has_vo = wd.new_vo.is_file()
    has_source = wd.source.is_file() and Path(wd.source.read_text(encoding="utf-8").strip()).is_file()
    prompt = ADVISE_PROMPT.format(
        iw=iw, ih=ih, n_frames=len(frames),
        frame_list="\n".join(f"  {p}" for p in frames),
        relipsync_ok="" if has_vo and has_source else "(NOT available for this dub)",
        vo_ok="" if has_vo and has_source else "(NOT available for this dub)",
        complaint=complaint)
    cenv = current_app.config["JOB_RUNNER"]._job_env_factory()
    cenv.pop("CLAUDECODE", None)
    try:
        r = subprocess.run([claude_exe, "-p", "--model", "sonnet", "--allowedTools", "Read"],
                           input=prompt, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, cwd=str(work), env=cenv)
    except subprocess.TimeoutExpired:
        abort(504, "the advisor took too long — try again")
    out = r.stdout or ""
    i, j2 = out.find("{"), out.rfind("}")
    if r.returncode != 0 or i < 0 or j2 <= i:
        abort(502, f"advisor failed: {(r.stderr or out)[:300]}")
    try:
        plan = json.loads(out[i:j2 + 1])
    except json.JSONDecodeError:
        abort(502, "advisor reply was not valid JSON — try again")

    action = plan.get("action") if plan.get("action") in (
        "object", "visual", "relipsync", "refit", "remux", "renorm") else "visual"
    if action in ("relipsync", "refit", "remux") and not (has_vo and has_source):
        action = "visual" if has_source else "renorm"
    if action == "object" and not has_source:
        action = "renorm"

    obj_raw = plan.get("object_boxes") or []
    if action == "object":
        if not any(isinstance(b, dict) for b in obj_raw):
            action = "visual"       # nothing located → fall back to the broad repair
    boxes = [b for b in (plan.get("boxes") or []) if isinstance(b, dict)]
    box = None
    track = bool(plan.get("track"))
    if boxes:
        W, H = int(iw / scale), int(ih / scale)
        if track and len(boxes) > 1:
            # tracked box follows the face per frame → size it like ONE lip box
            # (the median), not the union of every position (that keeps too much dub)
            med = lambda k: sorted(b[k] for b in boxes)[len(boxes) // 2]
            w0, h0 = med("w") / scale, med("h") / scale
            cx = med("x") / scale + w0 / 2
            cy = med("y") / scale + h0 / 2
            x0, y0, x1, y1 = cx - w0 / 2, cy - h0 / 2, cx + w0 / 2, cy + h0 / 2
        else:
            x0 = min(b["x"] for b in boxes) / scale
            y0 = min(b["y"] for b in boxes) / scale
            x1 = max(b["x"] + b["w"] for b in boxes) / scale
            y1 = max(b["y"] + b["h"] for b in boxes) / scale
        px, py = (x1 - x0) * 0.2, (y1 - y0) * 0.2
        bx = max(0, int(x0 - px)); by = max(0, int(y0 - py))
        box = {"x": bx, "y": by,
               "w": max(24, min(W - bx, int(x1 - x0 + 2 * px))),
               "h": max(24, min(H - by, int(y1 - y0 + 2 * py)))}
    if action == "visual" and not box:
        abort(502, "the advisor could not locate the lips — try drawing the box")

    result = {"action": action, "box": box, "track": bool(plan.get("track")),
              "explanation": plan.get("explanation") or "", "ts": time.time()}
    eng = current_app.config["REPAIR_ENGINES_DIR"]
    venv_py = str(Path(current_app.config["CV_VENV_PY"]))

    if action == "object":
        # per-sample obj + lips boxes (scaled to full-res) with their frame indices —
        # exactly what object_repair.py needs
        lips_raw = plan.get("boxes") or []
        samples = []
        for k, j in enumerate(indices):
            def up(b):
                if not isinstance(b, dict):
                    return None
                return {"x": int(b["x"] / scale), "y": int(b["y"] / scale),
                        "w": int(b["w"] / scale), "h": int(b["h"] / scale)}
            samples.append({"j": j,
                            "obj": up(obj_raw[k]) if k < len(obj_raw) else None,
                            "lips": up(lips_raw[k]) if k < len(lips_raw) else None})
        result["samples"] = samples
        rois = work / "object-rois.json"
        rois.write_text(json.dumps({"samples": samples}, indent=1), encoding="utf-8")
        cmd = [venv_py, str(eng / "object_repair.py"), "--work", str(work),
               "--rois", str(rois), "--preview-at", "-1"]
        try:
            pr2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=280, env=env)
            if pr2.returncode == 0:
                result["img"] = f"/media/output/script-swap/{stem}/preview-object.jpg"
                for ln in pr2.stdout.splitlines():
                    if ln.startswith("ALIGN:"):
                        try:
                            result["align"] = json.loads(ln[6:].strip())
                        except json.JSONDecodeError:
                            pass
            else:
                err = next((ln for ln in (pr2.stdout or "").splitlines()
                            if ln.startswith("ERROR:")), "")
                result["warning"] = err or "preview failed — you can still run the repair"
        except subprocess.TimeoutExpired:
            result["warning"] = "preview took too long — you can still run the repair"
        return jsonify(result)

    if action == "visual" and box and has_source:
        cmd = [venv_py, str(eng / "visual_repair.py"), "--work", str(work),
               "--box", str(box["x"]), str(box["y"]), str(box["w"]), str(box["h"]),
               "--preview-at", "2.0"]
        try:
            pr2 = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=90, env=env)
            if pr2.returncode == 0:
                result["img"] = f"/media/output/script-swap/{stem}/preview-visual.jpg"
                for ln in pr2.stdout.splitlines():
                    if ln.startswith("ALIGN:"):
                        try:
                            result["align"] = json.loads(ln[6:].strip())
                        except json.JSONDecodeError:
                            pass
        except subprocess.TimeoutExpired:
            pass
    return jsonify(result)


# ---------------------------------------------------------------- upload helpers (ZNCC)

def _sparse_thumbs(path: Path, env: dict, side: int = 64, rate: str = "0.5"):
    """Tiny grayscale thumbnails every 1/rate seconds — a cheap content
    fingerprint. Byte-faithful move of server.py L2767-2778 (numpy is a
    lazy import, as in the monolith)."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"fps={rate},scale={side}:{side}", "-pix_fmt", "gray",
         "-f", "rawvideo", "pipe:1"],
        capture_output=True, env=env)
    n = len(r.stdout) // (side * side)
    if n == 0:
        return None
    import numpy as np
    return np.frombuffer(r.stdout[:n * side * side], np.uint8).reshape(n, side, side)


def _video_meta(path: Path, env: dict):
    """(width, height, duration) via ffprobe, or None. Move of
    server.py L2781-2792 (bare ``ffprobe``, on PATH via the job env)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, env=env)
    try:
        d = json.loads(r.stdout)
        return (int(d["streams"][0]["width"]), int(d["streams"][0]["height"]),
                float(d["format"].get("duration") or 0))
    except Exception:
        return None


def _find_original(dubbed: Path, uploads: Path, env: dict):
    """Content-match the dubbed video against the uploads library.
    Returns (path, score) of the best candidate, or (None, best_score).
    Byte-faithful move of server.py L2795-2842."""
    import numpy as np
    meta = _video_meta(dubbed, env)
    if not meta:
        return None, 0.0
    W, H, dur = meta
    thumbs_d = _sparse_thumbs(dubbed, env)
    if thumbs_d is None:
        return None, 0.0
    flat_d = thumbs_d.reshape(thumbs_d.shape[0], -1).astype(np.float32)
    flat_d -= flat_d.mean(axis=1, keepdims=True)
    norm = np.linalg.norm(flat_d, axis=1, keepdims=True)
    flat_d /= np.maximum(norm, 1e-6)

    exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    cands = sorted((p for p in uploads.iterdir()
                    if p.is_file() and p.suffix.lower() in exts),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:40]
    hits = []            # (score, bitrate, path)
    best_score = 0.0
    for cand in cands:
        m = _video_meta(cand, env)
        # the original must match resolution and be at least as long (dubs get tail-truncated)
        if not m or m[0] != W or m[1] != H or m[2] < dur - 1.0:
            continue
        thumbs_c = _sparse_thumbs(cand, env)
        if thumbs_c is None:
            continue
        k = min(thumbs_d.shape[0], thumbs_c.shape[0])
        if k < 2:
            continue
        flat_c = thumbs_c[:k].reshape(k, -1).astype(np.float32)
        flat_c -= flat_c.mean(axis=1, keepdims=True)
        nc = np.linalg.norm(flat_c, axis=1, keepdims=True)
        flat_c /= np.maximum(nc, 1e-6)
        score = float((flat_d[:k] * flat_c).sum(axis=1).mean())
        best_score = max(best_score, score)
        if score >= 0.80:
            hits.append((score, cand.stat().st_size / max(1.0, m[2]), cand))
    if not hits:
        return None, best_score
    # among near-equal matches (duplicate copies of the same footage — anything
    # within a 0.05 score band), take the highest-bitrate one: repairs should
    # pull the sharpest pixels available
    hits.sort(key=lambda t: (round(t[0] * 20), t[1]), reverse=True)
    return hits[0][2], hits[0][0]


# -- 6. POST /api/dubsync/upload (drag & drop + auto-find original) ------

@dubsync_bp.post("/api/dubsync/upload")
def api_dubsync_upload():
    """Drag & drop repair: drop the damaged (dubbed) video and the backend
    FINDS its original automatically by content-matching against the
    uploads library. If no confident match exists, the caller is asked to
    supply the original too.

    Pure relocation of server.py L2845-2887.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    uploads = current_app.config["UPLOADS"]
    swap_work = autovsl / "output" / "script-swap"
    exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
    dubbed = request.files.get("dubbed")
    original = request.files.get("original")
    if not dubbed or not dubbed.filename:
        abort(400, "drop the damaged video")
    if Path(dubbed.filename).suffix.lower() not in exts:
        abort(400, f"unsupported type: {dubbed.filename}")
    if original and original.filename and \
            Path(original.filename).suffix.lower() not in exts:
        abort(400, f"unsupported type: {original.filename}")

    base = secure_filename(Path(dubbed.filename).stem).strip(".-_") or f"repair-{time.strftime('%Y%m%d-%H%M%S')}"
    stem, n = base, 2
    while (swap_work / stem).exists():
        stem = f"{base}-{n}"
        n += 1
    wd = DubWorkdir(autovsl, stem)
    wd.dir.mkdir(parents=True)
    dubbed.save(wd.final)

    if original and original.filename:                       # manual pair (fallback path)
        uploads.mkdir(exist_ok=True)
        oname = secure_filename(Path(original.filename).name) or f"{stem}-original.mp4"
        opath, n = uploads / oname, 2
        while opath.exists():
            opath = uploads / f"{Path(oname).stem}-{n}{Path(oname).suffix}"
            n += 1
        original.save(opath)
        wd.source.write_text(str(opath), encoding="utf-8")
        return jsonify({"stem": stem, "source": opath.name, "auto": False})

    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    match, score = _find_original(wd.final, uploads, env)
    if match is None:
        shutil.rmtree(wd.dir, ignore_errors=True)            # nothing usable was created
        abort(422, f"couldn't find the original in your library (best match {score:.0%}) "
                   "— drop the original video too")
    wd.source.write_text(str(match), encoding="utf-8")
    return jsonify({"stem": stem, "source": match.name, "auto": True,
                    "score": round(score, 3)})


# ---------------------------------------------------------------- wire-up

def register_dubsync(app) -> None:
    """Register the DubSync Blueprint and stash the repair-engine dir +
    the dubbing-studio venv/lipsync paths.

    Config keys set:
      - ``REPAIR_ENGINES_DIR`` — the backend-app/engines/ directory
        (the byte-identical repair engines moved in E1), resolved
        relative to this package so it follows the app, not a config
        path. Mirrors server.py's ``APP_DIR / "engines"``.
      - ``DUB_VENV_PY`` — the dubbing-studio venv python (relipsync's
        ``--dub-python``). Mirrors ``CONFIG["venvs"]["dub"]``.
      - ``LIPSYNC_PY`` — dubbing-studio/lipsync.py (relipsync's
        ``--lipsync``). Mirrors ``CONFIG["engines"]["lipsync"]``.

    Must register AFTER register_dubbing (which sets ``CV_VENV_PY``).
    ``READY_DIR`` + ``AUTOVSL_ROOT`` + ``JOB_RUNNER`` come from app.py.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    app.config["REPAIR_ENGINES_DIR"] = backend_dir / "engines"
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["DUB_VENV_PY"] = str(
        autovsl / ".." / "dubbing-studio" / "venv" / "Scripts" / "python.exe"
    )
    app.config["LIPSYNC_PY"] = str(
        autovsl / ".." / "dubbing-studio" / "lipsync.py"
    )
    app.register_blueprint(dubsync_bp)
