"""File / utility route module (B13 S2).

Pure move of seven utility endpoints from video-studio/app/server.py:

  1. ``GET  /api/file``            (L1742-1751) — read a text file
     (md/json/txt/jsonl) from inside the data root.
  2. ``GET  /api/thumb/<path>``    (L2253-2268) — cached poster frame
     for an upload (ffmpeg, jpeg).
  3. ``POST /api/edit``            (L2154-2180) — ffmpeg cut/zoom → a new
     file in output/edits/ (spawns a job).
  4. ``POST /api/upload``          (L657-675)   — multipart video upload
     into uploads/ (never overwrites).
  5. ``DELETE /api/upload``        (L915-949)   — bundle a video + its
     transcript + dub workdir into .trash (409 if a job is running).
  6. ``POST /api/trash/restore``   (L1222-1253) — restore a bundle/file
     from .trash to where it came from.
  7. ``DELETE /api/trash``         (L1256-1269) — purge a .trash item.

No worker threads here — ``edit`` spawns the ffmpeg job via
``runner.run`` (args baked in request context); everything else is
synchronous. So no current_app-in-thread concern.

HELPERS:
  - ``safe_video_path`` (used by ``edit``) was promoted qc.py → common.py
    in this slice — /api/edit is its 2nd, cross-subsystem consumer.
  - ``read_json`` + ``ffmpeg`` come from services.helpers.common.
  - ``.trash`` paths + the media-upload ext set are derived/defined
    locally (small, file-subsystem-owned).

DEPENDENCIES:
  - services.helpers.common: safe_video_path, read_json, ffmpeg
  - services.jobs:           jobs + jobs_lock (edit job; delete 409 guard)
  - services.workdir:        DubWorkdir (the workdir piece in delete-bundle)
  - app.config:              AUTOVSL_ROOT, UPLOADS, TRANSCRIPTS, FFMPEG_BIN,
                             JOB_RUNNER

server.py is unchanged. These blocks stay at their original lines until
the cutover. Rule 16.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from services.helpers.common import ffmpeg, read_json, safe_video_path
from services.jobs import jobs, jobs_lock
from services.workdir import DubWorkdir


# Media types accepted by POST /api/upload (server.py L90 MEDIA_UPLOAD_EXTS).
MEDIA_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav"}
# Text types /api/file will read (server.py L1747).
FILE_TEXT_EXTS = (".md", ".json", ".txt", ".jsonl")


files_bp = Blueprint("files", __name__)


# -- 1. GET /api/file -----------------------------------------------------

@files_bp.get("/api/file")
def api_file():
    """Return a text file from inside the repo (markdown/json/txt only).
    Pure relocation of server.py L1742-1751."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    rel = request.args.get("path", "")
    target = (autovsl / rel).resolve()
    if not str(target).startswith(str(autovsl)) or target.suffix not in FILE_TEXT_EXTS:
        abort(403)
    if not target.is_file():
        abort(404)
    return jsonify({"path": rel, "content": target.read_text(encoding="utf-8", errors="replace")})


# -- 2. GET /api/thumb/<path> --------------------------------------------

@files_bp.get("/api/thumb/<path:name>")
def api_thumb(name):
    """Cached poster frame for an upload (so the library shows real thumbnails).
    Pure relocation of server.py L2253-2268."""
    uploads = current_app.config["UPLOADS"]
    src = uploads / Path(name).name
    if not src.is_file():
        abort(404)
    thumbs = current_app.config["AUTOVSL_ROOT"] / "output" / "qc" / "cache" / "thumbs"
    out = thumbs / f"{src.stem}-{int(src.stat().st_mtime)}.jpg"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [ffmpeg(current_app.config["FFMPEG_BIN"]), "-y", "-loglevel", "error",
             "-ss", "1", "-i", str(src), "-frames:v", "1", "-vf", "scale=270:-2",
             "-q:v", "4", str(out)],
            env=current_app.config["JOB_RUNNER"]._job_env_factory(), timeout=60)
    if not out.is_file():
        abort(500, "thumbnail failed")
    return send_file(out, mimetype="image/jpeg", max_age=3600)


# -- 3. POST /api/edit (ffmpeg cut/zoom job) -----------------------------

@files_bp.post("/api/edit")
def api_edit():
    """Cut & zoom editor: trim [start,end] + optional center zoom → a NEW
    file in output/edits/. Pure relocation of server.py L2154-2180."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    b = request.get_json(force=True)
    src = safe_video_path(b.get("path", ""), autovsl)
    try:
        start, end = float(b.get("start") or 0), float(b.get("end") or 0)
        zoom = max(1.0, min(3.0, float(b.get("zoom") or 1)))
    except (TypeError, ValueError):
        abort(400, "start/end/zoom must be numbers")
    if end <= start:
        abort(400, "end must be after start")
    out_dir = autovsl / "output" / "edits"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{src.stem}-cut-{time.strftime('%H%M%S')}.mp4"
    cmd = [ffmpeg(current_app.config["FFMPEG_BIN"]), "-y", "-ss", str(start), "-to", str(end), "-i", str(src)]
    if zoom > 1:
        cmd += ["-vf", f"crop=iw/{zoom}:ih/{zoom}:(iw-iw/{zoom})/2:(ih-ih/{zoom})/2,scale=iw*{zoom}:ih*{zoom}"]
    cmd += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart", str(out)]
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "edit", "slug": src.stem,
                    "label": f"✂ Edit — {src.name} ({start:.0f}-{end:.0f}s{', zoom ×' + str(zoom) if zoom > 1 else ''})",
                    "status": "running", "lines": [], "returncode": None,
                    "started": time.time(), "ended": None}
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "output": str(out.relative_to(autovsl)).replace("\\", "/")})


# -- 4. POST /api/upload (multipart video upload) ------------------------

@files_bp.post("/api/upload")
def api_upload():
    """Upload a video into uploads/ (never overwrites — an in-flight video
    keeps its pipeline). Pure relocation of server.py L657-675."""
    uploads = current_app.config["UPLOADS"]
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    orig = Path(f.filename)
    ext = orig.suffix.lower()
    if ext not in MEDIA_UPLOAD_EXTS:
        abort(400, f"unsupported type — allowed: {', '.join(sorted(MEDIA_UPLOAD_EXTS))}")
    # secure_filename strips non-ASCII (e.g. Hebrew names) — fall back to a timestamp name
    base = secure_filename(orig.stem).strip(".-_") or f"upload-{time.strftime('%Y%m%d-%H%M%S')}"
    name, n = f"{base}{ext}", 2
    while (uploads / name).exists():   # NEVER overwrite — an in-flight video keeps its pipeline
        name = f"{base}-{n}{ext}"
        n += 1
    uploads.mkdir(exist_ok=True)
    f.save(uploads / name)
    return jsonify({"name": name, "size": (uploads / name).stat().st_size,
                    "renamed": name != orig.name})


# -- 5. DELETE /api/upload (bundle the whole pipeline into .trash) -------

@files_bp.delete("/api/upload")
def api_upload_delete():
    """Remove a video and its whole pipeline (transcript, scripts, dubs) into
    .trash as one bundle. Pure relocation of server.py L915-949."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    uploads = current_app.config["UPLOADS"]
    transcripts = current_app.config["TRANSCRIPTS"]
    trash = autovsl / ".trash"
    trash_index = trash / "index.json"
    fname = Path(request.args.get("file") or "").name
    src = uploads / fname
    if not fname or not src.is_file():
        abort(404, "upload not found")
    stem = Path(fname).stem
    with jobs_lock:  # deleting mid-job rips the work dir out from under the pipeline
        for j in jobs.values():
            if j["status"] == "running" and j["slug"] in (fname, stem):
                abort(409, f"a {j['action']} job is still running on this video — wait for it to finish")
    trash.mkdir(exist_ok=True)
    bundle_name = f"upload-{secure_filename(stem) or 'video'}-{time.strftime('%Y%m%d-%H%M%S')}"
    bundle = trash / bundle_name
    bundle.mkdir()
    pieces = {
        "video": (src, f"uploads/{fname}"),
        "transcript-md": (transcripts / f"{stem}.md", f"uploads/transcripts/{stem}.md"),
        "transcript-json": (transcripts / f"{stem}.json", f"uploads/transcripts/{stem}.json"),
        "workdir": (DubWorkdir(autovsl, stem).dir, f"output/script-swap/{stem}"),
        "original": (uploads / ".originals" / fname, f"uploads/.originals/{fname}"),
        "boxjson": (uploads / ".originals" / f"{stem}.box.json", f"uploads/.originals/{stem}.box.json"),
    }
    manifest = {}
    for key, (path, original) in pieces.items():
        if path.exists():
            shutil.move(str(path), str(bundle / key))
            manifest[key] = original
    (bundle / "bundle.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    idx = read_json(trash_index) or {}
    idx[bundle_name] = {"original": f"uploads/{fname} (+ transcript & dub work)",
                        "deleted": time.time(), "bundle": True}
    trash_index.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"moved_to": f".trash/{bundle_name}", "pieces": list(manifest)})


# -- 6. POST /api/trash/restore ------------------------------------------

@files_bp.post("/api/trash/restore")
def api_trash_restore():
    """Restore a trashed item (bundle or single file) to where it lived.
    Pure relocation of server.py L1222-1253."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    trash = autovsl / ".trash"
    trash_index = trash / "index.json"
    name = Path(request.get_json(force=True).get("name") or "").name
    idx = read_json(trash_index) or {}
    meta = idx.get(name)
    src = trash / name
    if not meta or not src.exists():
        abort(404, "not found in trash")
    bundle_manifest = read_json(src / "bundle.json") if src.is_dir() else None
    if bundle_manifest:  # multi-piece upload bundle — put every piece back where it lived
        primary = bundle_manifest.get("video")
        if primary and (autovsl / primary).exists():
            abort(409, f"cannot restore — {primary} already exists")
        for key, original in bundle_manifest.items():
            piece = src / key
            dest = autovsl / original
            if piece.exists() and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(piece), str(dest))
        shutil.rmtree(src, ignore_errors=True)
        idx.pop(name, None)
        trash_index.write_text(json.dumps(idx, indent=1), encoding="utf-8")
        return jsonify({"restored_to": primary or meta["original"]})

    dest = autovsl / meta["original"]
    if dest.exists():
        abort(409, f"cannot restore — {meta['original']} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    idx.pop(name, None)
    trash_index.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"restored_to": meta["original"]})


# -- 7. DELETE /api/trash (purge) ----------------------------------------

@files_bp.delete("/api/trash")
def api_trash_purge():
    """Permanently delete a trashed item. Pure relocation of server.py L1256-1269."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    trash = autovsl / ".trash"
    trash_index = trash / "index.json"
    name = Path(request.args.get("name") or "").name
    idx = read_json(trash_index) or {}
    src = trash / name
    if name not in idx or not src.exists():
        abort(404, "not found in trash")
    if src.is_dir():
        shutil.rmtree(src)
    else:
        src.unlink()
    idx.pop(name, None)
    trash_index.write_text(json.dumps(idx, indent=1), encoding="utf-8")
    return jsonify({"purged": name})


# ---------------------------------------------------------------- wire-up

def register_files(app) -> None:
    """Register the file/utility Blueprint. No config keys of its own —
    everything derives from AUTOVSL_ROOT / UPLOADS / TRANSCRIPTS /
    FFMPEG_BIN (app.py) + JOB_RUNNER."""
    app.register_blueprint(files_bp)
