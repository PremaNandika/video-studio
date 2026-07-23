"""Library helpers — read-only state queries for the Library tab.

Pure relocation of video-studio/app/server.py L1494-1573 +
L1621-1651 + L269-277. Five pure-data helpers, no I/O mutation,
no subprocess, no state. Each one reads files / the jobs dict
and returns a list of dicts (or a single dict) for the frontend
to render.

WHY services/helpers/ (not services/library.py):
  The user established the rule (2026-07-20) that all future
  helpers go in ``services/helpers/``. These five are the
  "library-flavored" ones, but the directory is the umbrella
  for every helper that isn't stateful or class-shaped
  (Rule 17: "Route handlers are functions, engine wrappers
  are functions, subprocess helpers are functions").

WHAT MOVES (5 helpers, ~120 lines byte-equivalent):
  upload_active_job   L1494-1512  — find the running/last-failed
                                    pipeline job for an upload
  uploads_state       L1515-1551  — the per-upload row in the
                                    Library tab (transcript, dub,
                                    caption, recaption, cleanup)
  dub_versions        L1554-1573  — all dub takes for a stem
                                    (final + archived), with the
                                    models that made them
  vsl_state           L1621-1651  — one VSL product's manifest +
                                    segments + output
  trash_state         L269-277    — items in the .trash dir

WHAT IS NOT HERE (deferred):
  - api_overview itself (the route) — routes/library.py
  - api_creator_library, api_bank, api_file, media — different
    blueprints; not "library" in the user-facing sense even
    though the plan groups them

DEPENDENCIES (read from app.config at call time, not import time):
  UPLOADS        — autoVSL/uploads
  TRANSCRIPTS    — UPLOADS/transcripts
  READY_DIR      — exports_dir / "liitt testimonial Ready"
  DESKTOP_VSLS   — ~/Desktop / "litt VSL's"
  AUTOVSL_ROOT   — the autoVSL/ repo root (= ROOT in server.py)
  TRASH, TRASH_INDEX — .trash dir and its index

server.py is unchanged. The 5 helpers stay at their original
lines until the entire library subsystem is retired. Rule 16.
"""
from __future__ import annotations

from flask import current_app

from services.jobs import jobs, jobs_lock
from services.spend import read_json  # safe JSON loader (no exception on bad/missing file)


# Pure relocation of server.py L90 — the set of file extensions
# considered "media uploads" in uploads/. Used by uploads_state to
# filter the directory listing.
MEDIA_UPLOAD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mp3", ".m4a", ".wav"}


def upload_active_job(name: str, stem: str) -> dict | None:
    """Find a running/last-failed pipeline job for this upload
    (read-only job inspection). Returns the job dict + the dub
    substage if applicable, or None if no pipeline job touched it.

    Pure relocation of server.py L1494-1512.
    """
    best = None
    with jobs_lock:
        for j in jobs.values():
            if j["action"] not in ("transcribe", "dub") or j["slug"] not in (name, stem):
                continue
            if best is None or j["started"] > best["started"]:
                best = j
        if not best:
            return None
        substage = None
        if best["action"] == "dub":
            for line in reversed(best["lines"]):
                if line.startswith("=== stage:"):
                    substage = line.replace("=== stage:", "").strip(" =")
                    break
        return {"id": best["id"], "action": best["action"], "status": best["status"],
                "started": best["started"], "ended": best["ended"], "substage": substage}


def dub_versions(stem: str) -> list[dict]:
    """All dub takes for an upload (current final + archived),
    with the models that made them.

    Pure relocation of server.py L1554-1573. Note: this uses
    the swap_work path from app.config, not the DubWorkdir class
    — the original code uses string concatenation; we keep that
    for byte-equivalence. When DubWorkdir behavior methods land
    (B5+, when a blueprint actually needs them), this can be
    rewritten to use DubWorkdir.iter_takes().
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    work = autovsl / "output" / "script-swap" / stem
    if not work.is_dir():
        return []
    versions_meta = read_json(work / "versions.json") or {}
    cfg = read_json(work / "dub-config.json") or {}
    out = []
    for f in sorted(work.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.startswith(("new-vo", "final-captioned")):
            continue   # not takes: VO audio-carrier / captioned derivative
        meta = cfg if f.name == "final.mp4" else versions_meta.get(f.name, {})
        out.append({
            "file": f.name,
            "path": f"output/script-swap/{stem}/{f.name}",
            "mtime": f.stat().st_mtime, "size": f.stat().st_size,
            "tts": meta.get("tts"), "tier": meta.get("tier"),
            "current": f.name == "final.mp4",
        })
    return out


def uploads_state() -> list[dict]:
    """The per-upload row in the Library tab: transcript, dub,
    caption, recaption, cleanup status, plus the active job if any.

    Pure relocation of server.py L1515-1551.
    """
    items = []
    uploads = current_app.config["UPLOADS"]
    transcripts = current_app.config["TRANSCRIPTS"]
    autovsl = current_app.config["AUTOVSL_ROOT"]
    if uploads.is_dir():
        for p in sorted(uploads.iterdir()):
            if p.is_file() and p.suffix.lower() in MEDIA_UPLOAD_EXTS:
                md = transcripts / f"{p.stem}.md"
                work = autovsl / "output" / "script-swap" / p.stem
                final = work / "final.mp4"
                script = work / "script-edited.txt"
                vo = work / "new-vo.mp3"
                items.append({
                    "name": p.name, "stem": p.stem,
                    "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                    "is_video": p.suffix.lower() not in (".mp3", ".m4a", ".wav"),
                    "transcript": md.is_file(),
                    "transcript_path": f"uploads/transcripts/{p.stem}.md" if md.is_file() else None,
                    "transcript_mtime": md.stat().st_mtime if md.is_file() else None,
                    "script_edited": script.is_file(),
                    "script_mtime": script.stat().st_mtime if script.is_file() else None,
                    "voice_cloned": (work / "voice.json").is_file(),
                    "vo_ready": vo.is_file(),
                    "vo_mtime": vo.stat().st_mtime if vo.is_file() else None,
                    "dub_final": f"output/script-swap/{p.stem}/final.mp4" if final.is_file() else None,
                    "dub_mtime": final.stat().st_mtime if final.is_file() else None,
                    "dub_versions": dub_versions(p.stem),
                    "cleaned": (uploads / ".originals" / p.name).is_file(),
                    "recaptioned": f"output/recaption/{p.stem}/captioned.mp4"
                                   if (autovsl / "output" / "recaption" / p.stem / "captioned.mp4").is_file() else None,
                    "recaptioned_stale": (autovsl / "output" / "recaption" / p.stem / "captioned.mp4").is_file()
                                         and p.stat().st_mtime > (autovsl / "output" / "recaption" / p.stem / "captioned.mp4").stat().st_mtime,
                    "captioned": f"output/script-swap/{p.stem}/final-captioned.mp4"
                                 if (work / "final-captioned.mp4").is_file() else None,
                    "captioned_stale": (work / "final-captioned.mp4").is_file() and final.is_file()
                                       and final.stat().st_mtime > (work / "final-captioned.mp4").stat().st_mtime,
                    "active": upload_active_job(p.name, p.stem),
                })
    return items


def vsl_state(slug: str) -> dict:
    """One VSL product's manifest + segments + output.

    Pure relocation of server.py L1621-1651.
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    vsl_dir = autovsl / "vsls" / slug
    timeline = read_json(vsl_dir / "timeline.json") or {}
    media_root = autovsl / timeline.get("media_root", f"vsls/{slug}/media")
    segments = []
    for seg in timeline.get("segments", []):
        segments.append({
            "shot": seg.get("shot"),
            "vo_text": seg.get("vo_text", ""),
            "notes": seg.get("notes", ""),
            "video": seg.get("video"),
            "vo": seg.get("vo"),
            "video_ok": bool(seg.get("video")) and (media_root / seg["video"]).is_file(),
            "vo_ok": bool(seg.get("vo")) and (media_root / seg["vo"]).is_file(),
        })
    music_file = (timeline.get("music") or {}).get("file")
    output = autovsl / "output" / f"{slug}.mp4"
    return {
        "slug": slug,
        "name": timeline.get("name", slug),
        "media_root": timeline.get("media_root", f"vsls/{slug}/media"),
        "target_duration": timeline.get("target_duration_seconds"),
        "aspect_ratio": timeline.get("aspect_ratio"),
        "segments": segments,
        "music_ok": bool(music_file) and (media_root / music_file).is_file(),
        "music_file": music_file,
        "has_prompts": (vsl_dir / "kling-shots.json").is_file(),
        "output_exists": output.is_file(),
        "output_mtime": output.stat().st_mtime if output.is_file() else None,
        "output_size": output.stat().st_size if output.is_file() else None,
    }


def trash_state() -> list[dict]:
    """Items in the .trash dir, newest-deleted first.

    Pure relocation of server.py L269-277. Uses TRASH and
    TRASH_INDEX from app.config (derived in create_app).
    """
    autovsl = current_app.config["AUTOVSL_ROOT"]
    trash = autovsl / ".trash"
    trash_index = trash / "index.json"
    idx = read_json(trash_index) or {}
    items = []
    for name, meta in sorted(idx.items(), key=lambda kv: kv[1].get("deleted", 0), reverse=True):
        p = trash / name
        if p.exists():
            items.append({"name": name, "original": meta.get("original", "?"),
                          "deleted": meta.get("deleted"), "is_dir": p.is_dir()})
    return items
