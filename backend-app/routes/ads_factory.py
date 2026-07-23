"""Ads Factory route module — /api/run residual (B13 S3) + creator/VSL (B13 S4).

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

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request

from services.helpers.ads_factory_helpers import (
    CREATOR_VIDEO_EXTS, MANIFEST_STAGES, PRODUCT_DIRS, VIDEO_MODELS,
    _RUN_ACTIONS, _check_slug, _spawn, _spawn_shell, library_lock,
    library_meta, valid_slug,
)
from services.helpers.common import soft_delete
from services.helpers.transcripts_helpers import transcript_plain_text
from services.jobs import jobs, jobs_lock
from services.prompts import BUILD_PROMPT


ads_factory_bp = Blueprint("ads_factory", __name__)


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


# ================================================================ CREATOR / VSL (B13 S4)
# The product/research/library CRUD + build-vsl for the Ads Factory tab.
# Folded into this module (same tab/domain as the /api/run actions above).

# -- build-vsl worker (Claude one-shot → VSL package) --------------------

def build_vsl_worker(job_id: str, vsl_slug: str, product: str, script_rel: str,
                     doc_rels: list[str], cfg: dict) -> None:
    """Design a VSL shot list with Claude and write the production package.

    Pure relocation of server.py L993-1084. Runs on a bare daemon thread,
    so it takes a resolved ``cfg`` bundle (``autovsl`` / ``claude_exe`` /
    ``env``) as an arg and NEVER touches current_app (the B9 rule). The
    Claude call is an inline one-shot (BUILD_PROMPT); writes
    ``vsls/<slug>/`` (kling-shots.json, elevenlabs-vo.json, timeline.json,
    brief.md).
    """
    job = jobs[job_id]
    autovsl = cfg["autovsl"]

    def log(line: str) -> None:
        with jobs_lock:
            job["lines"].append(line)

    try:
        script_path = autovsl / "products" / product / script_rel
        script = script_path.read_text(encoding="utf-8", errors="replace")[:8000]
        context_parts = []
        for rel in doc_rels[:6]:
            p = (autovsl / rel).resolve()
            if str(p).startswith(str(autovsl)) and p.suffix == ".md" and p.is_file():
                context_parts.append(f"--- {rel} ---\n" + p.read_text(encoding="utf-8", errors="replace")[:3500])
        offer = autovsl / "products" / product / "offer.md"
        if offer.is_file():
            context_parts.insert(0, "--- product offer ---\n" + offer.read_text(encoding="utf-8", errors="replace")[:2500])
        context = ("CONTEXT:\n" + "\n\n".join(context_parts)) if context_parts else "CONTEXT: (none provided)"

        log(f"Building VSL '{vsl_slug}' from {script_rel} with {len(context_parts)} context doc(s)")
        log("Asking Claude to design the shot list (this takes a minute or two)...")

        env = dict(cfg["env"])
        env.pop("CLAUDECODE", None)
        result = subprocess.run(
            [cfg["claude_exe"], "-p", "--model", "opus",
             "--disallowedTools", "Write,Edit,Bash,NotebookEdit,WebFetch,WebSearch,Task"],
            input=BUILD_PROMPT.format(context=context, script_name=script_rel, script=script),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=600, cwd=str(autovsl), env=env,
        )
        out = (result.stdout or "").strip()
        if result.returncode != 0 or not out:
            raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {(result.stderr or '')[:300]}")
        if out.startswith("```"):
            out = out.split("```")[1].lstrip("json").strip()
        start, end = out.find("{"), out.rfind("}")
        # scrub mojibake from Windows console decoding (em-dashes -> U+FFFD)
        plan = json.loads(out[start:end + 1].replace("�", "-"))
        shots = plan.get("shots") or []
        if not (3 <= len(shots) <= 14):
            raise RuntimeError(f"unexpected shot count: {len(shots)}")

        vdir = autovsl / "vsls" / vsl_slug
        (vdir / "media" / "video").mkdir(parents=True)
        (vdir / "media" / "audio").mkdir(parents=True)
        (vdir / "media" / "music").mkdir(parents=True)

        (vdir / "kling-shots.json").write_text(json.dumps({
            "settings": {"negative_prompt": plan.get("negative_prompt", ""), "aspect_ratio": "9:16"},
            "shots": [{"id": s["id"], "filename": f"shot-{s['id']:02d}.mp4", "prompt": s["prompt"]}
                      for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "elevenlabs-vo.json").write_text(json.dumps({
            "voice": "en-US-ChristopherNeural", "voice_alternate": "en-US-GuyNeural",
            "lines": [{"id": s["id"], "filename": f"vo-{s['id']:02d}.mp3", "text": s["vo_text"]}
                      for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "timeline.json").write_text(json.dumps({
            "name": plan.get("name", vsl_slug),
            "aspect_ratio": "9:16", "fps": 30,
            "target_duration_seconds": len(shots) * 8,
            "media_root": f"vsls/{vsl_slug}/media",
            "tracks": {"video": 0, "voiceover": 1, "music": 2},
            "music": {"file": "music/background.mp3", "volume": 0.15,
                      "fade_in_seconds": 2, "fade_out_seconds": 3,
                      "duck_under_vo": True, "duck_volume": 0.08},
            "segments": [{"shot": s["id"], "video": f"video/shot-{s['id']:02d}.mp4",
                          "vo": f"audio/vo-{s['id']:02d}.mp3", "vo_text": s["vo_text"],
                          "notes": s.get("notes", "")} for s in shots],
        }, indent=2), encoding="utf-8")

        (vdir / "brief.md").write_text(
            f"# {plan.get('name', vsl_slug)}\n\n{plan.get('concept', '')}\n\n"
            f"- **Product:** {product}\n- **Script:** {script_rel}\n"
            f"- **Context docs:** {', '.join(doc_rels) or 'none'}\n"
            f"- **Built:** {time.strftime('%Y-%m-%d %H:%M')}\n", encoding="utf-8")

        log(f"Wrote {len(shots)} shots -> vsls/{vsl_slug}/ (kling-shots, elevenlabs-vo, timeline, brief)")
        log("Next: Generate VO (free) -> Generate video ($) -> Assemble (free)")
        job["returncode"] = 0
        job["status"] = "done"
    except Exception as exc:
        log(f"BUILD FAILED: {exc}")
        job["returncode"] = 1
        job["status"] = "failed"
    finally:
        job["ended"] = time.time()


@ads_factory_bp.post("/api/build-vsl")
def api_build_vsl():
    """Build a VSL production package from a product's approved script.
    Pure relocation of server.py L1087-1112."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    product = Path(body.get("product") or "").name
    script_rel = (body.get("script") or "").replace("\\", "/")
    docs = [d for d in (body.get("docs") or []) if isinstance(d, str)]
    vsl_slug = (body.get("name") or "").strip().lower().replace(" ", "-")
    if not valid_slug(product) or not (autovsl / "products" / product).is_dir():
        abort(400, "unknown product")
    script_path = (autovsl / "products" / product / script_rel).resolve()
    if not str(script_path).startswith(str(autovsl / "products" / product)) or not script_path.is_file():
        abort(400, "script not found")
    if not vsl_slug or not valid_slug(vsl_slug):
        abort(400, "VSL name must be letters/numbers/dashes")
    if (autovsl / "vsls" / vsl_slug).exists():
        abort(409, f"vsls/{vsl_slug} already exists — pick another name")
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "claude CLI not found")
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"id": job_id, "action": "build-vsl", "slug": vsl_slug,
                    "label": f"Build VSL — {vsl_slug}", "status": "running",
                    "lines": [], "returncode": None, "started": time.time(), "ended": None}
    cfg = {"autovsl": autovsl, "claude_exe": runner.claude_exe,
           "env": current_app.config["JOB_RUNNER"]._job_env_factory()}
    threading.Thread(target=build_vsl_worker,
                     args=(job_id, vsl_slug, product, script_rel, docs, cfg), daemon=True).start()
    return jsonify({"job_id": job_id})


# -- products & research CRUD --------------------------------------------

@ads_factory_bp.post("/api/product")
def api_product_create():
    """Create a product dir + manifest. Pure relocation of server.py L1121-1139."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    slug = (body.get("slug") or "").strip().lower()
    name = (body.get("name") or "").strip() or slug
    if not valid_slug(slug):
        abort(400, "slug must be letters/numbers/dashes only (e.g. night-mode)")
    pdir = autovsl / "products" / slug
    if pdir.exists():
        abort(409, f"product '{slug}' already exists")
    for d in PRODUCT_DIRS:
        (pdir / d).mkdir(parents=True)
    manifest = {
        "product": name, "slug": slug,
        "created": time.strftime("%Y-%m-%d"), "stage": 1,
        "stages": {key: {"status": "todo"} for key in MANIFEST_STAGES},
    }
    (pdir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return jsonify({"created": slug})


@ads_factory_bp.delete("/api/product/<slug>")
def api_product_delete(slug):
    """Trash a product. Pure relocation of server.py L1142-1149."""
    slug = Path(slug).name
    pdir = current_app.config["AUTOVSL_ROOT"] / "products" / slug
    if not pdir.is_dir():
        abort(404)
    return jsonify({"moved_to": soft_delete(pdir, f"product-{slug}")})


@ads_factory_bp.post("/api/research-doc")
def api_research_doc_create():
    """Create a research .md. Pure relocation of server.py L1152-1166."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    rel = (body.get("path") or "").strip().replace("\\", "/")
    content = body.get("content") or ""
    if not rel.endswith(".md"):
        rel += ".md"
    target = (autovsl / rel).resolve()
    if not str(target).startswith(str(autovsl / "research")):
        abort(400, "path must be under research/")
    if target.exists():
        abort(409, "that file already exists — pick another name")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or f"# {target.stem}\n\n", encoding="utf-8")
    return jsonify({"created": str(target.relative_to(autovsl)).replace("\\", "/")})


@ads_factory_bp.delete("/api/research-doc")
def api_research_doc_delete():
    """Trash a research .md. Pure relocation of server.py L1169-1178."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    rel = (request.args.get("path") or "").replace("\\", "/")
    target = (autovsl / rel).resolve()
    if not str(target).startswith(str(autovsl / "research")) or target.suffix != ".md":
        abort(400, "path must be a .md under research/")
    if not target.is_file():
        abort(404)
    return jsonify({"moved_to": soft_delete(target, f"doc-{target.stem}")})


@ads_factory_bp.get("/api/bank/<name>")
def api_bank(name):
    """Read a JSONL research bank. Pure relocation of server.py L1724-1739."""
    if name not in ("hooks", "angles", "pain-points", "broll"):
        abort(404)
    path = current_app.config["AUTOVSL_ROOT"] / "banks" / f"{name}.jsonl"
    entries = []
    if path.is_file():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return jsonify(entries)


# -- Creator page --------------------------------------------------------

@ads_factory_bp.get("/api/creator/library")
def api_creator_library():
    """Everything the Creator page needs: videos + pipeline state + tags.
    Pure relocation of server.py L2194-2224."""
    from services.workdir import DubWorkdir
    autovsl = current_app.config["AUTOVSL_ROOT"]
    uploads = current_app.config["UPLOADS"]
    transcripts = current_app.config["TRANSCRIPTS"]
    meta = library_meta(autovsl)
    vids = []
    if uploads.is_dir():
        for p in sorted(uploads.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not p.is_file() or p.suffix.lower() not in CREATOR_VIDEO_EXTS:
                continue
            stem = p.stem
            wd = DubWorkdir(autovsl, stem)
            final = wd.final
            m = meta.get(p.name) or {}
            orig_words = len(transcript_plain_text(stem).split())  # original speaker's word count → pace
            vids.append({
                "name": p.name, "stem": stem, "size": p.stat().st_size, "mtime": p.stat().st_mtime,
                "orig_words": orig_words,
                "cleaned": (uploads / ".originals" / p.name).is_file(),
                "transcript": (transcripts / f"{stem}.md").is_file(),
                "script": wd.script_edited.is_file(),
                "dub": f"output/script-swap/{stem}/final.mp4" if final.is_file() else None,
                "dub_mtime": final.stat().st_mtime if final.is_file() else None,
                "title": m.get("title") or "", "character": m.get("character") or "",
                "tags": m.get("tags") or [], "no_subs": bool(m.get("no_subs")),
                "approved": m.get("approved") or {},
            })
    return jsonify({
        "videos": vids,
        "characters": sorted({v["character"] for v in vids if v["character"]}),
        "tags": sorted({t for v in vids for t in v["tags"]}),
        "fal_spend": round(float(current_app.config["SPEND_LEDGER"].load().get("total", 0.0)), 2),
    })


@ads_factory_bp.post("/api/creator/meta")
def api_creator_meta():
    """Save per-video title/character/tags/no_subs/approved.
    Pure relocation of server.py L2227-2250."""
    b = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    uploads = current_app.config["UPLOADS"]
    fname = Path(b.get("name") or "").name
    if not fname or not (uploads / fname).is_file():
        abort(404, "upload not found")
    library_file = autovsl / "output" / "library.json"
    with library_lock:
        m = library_meta(autovsl)
        e = m.get(fname) or {}
        if "title" in b:
            e["title"] = (b.get("title") or "").strip()[:80]
        if "character" in b:
            e["character"] = (b.get("character") or "").strip()[:40]
        if "tags" in b:
            e["tags"] = [t.strip()[:24] for t in (b.get("tags") or []) if isinstance(t, str) and t.strip()][:12]
        if "no_subs" in b:
            e["no_subs"] = bool(b.get("no_subs"))
        if "approved" in b and isinstance(b.get("approved"), dict):
            e["approved"] = {k: bool(v) for k, v in b["approved"].items()
                             if k in ("clean", "script", "dub")}
        m[fname] = e
        library_file.parent.mkdir(parents=True, exist_ok=True)
        library_file.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return jsonify({"saved": fname})


@ads_factory_bp.post("/api/transcript-to-product")
def api_transcript_to_product():
    """Copy a transcript.md into research/<slug>/transcripts/.
    Pure relocation of server.py L678-692."""
    body = request.get_json(force=True)
    autovsl = current_app.config["AUTOVSL_ROOT"]
    transcripts = current_app.config["TRANSCRIPTS"]
    stem = Path(body.get("stem", "")).name
    slug = body.get("slug", "")
    src = transcripts / f"{stem}.md"
    if not stem or not src.is_file():
        abort(404, "transcript not found")
    if not (autovsl / "products" / slug).is_dir() and not (autovsl / "research" / slug).is_dir():
        abort(400, f"unknown product slug: {slug}")
    dest = autovsl / "research" / slug / "transcripts" / f"{stem}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return jsonify({"copied_to": str(dest.relative_to(autovsl)).replace("\\", "/")})


# ---------------------------------------------------------------- wire-up

def register_ads_factory(app) -> None:
    """Register the Ads Factory Blueprint and stash ``TRANSCRIBE_PY``.

    Owns the /api/run production actions (S3) + the creator/product/
    research/bank/build-vsl CRUD (S4). ``TRANSCRIBE_PY`` mirrors
    server.py's ``COURSE_PIPELINE / "transcribe.py"``, derived from
    AUTOVSL_ROOT. ``BASH`` comes from app.py (config.json); the whisper
    venv (``WHISPER_VENV_PY``) from register_captions; CLAUDE_RUNNER /
    SPEND_LEDGER / TRANSCRIPTS / UPLOADS from app.py. The S4 routes need
    no new config keys — products/research/banks/vsls + output/library.json
    all derive from AUTOVSL_ROOT at call time.
    """
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["TRANSCRIBE_PY"] = str(autovsl / ".." / "course_pipeline" / "transcribe.py")
    app.register_blueprint(ads_factory_bp)
