"""Brand Studio route module (B13 S5) — static brand image ads.

Pure move of 11 endpoints from video-studio/app/server.py:

  Brand (8):
    GET  /api/brand/health           (L3238) — ComfyUI/fonts/wordmark preflight
    GET  /api/brand/formats          (L3268) — templates + platforms/presets
    POST /api/brand/copy             (L3286) — inline Claude one-shot copy,
                                               3-attempt compliance-retry loop
    POST /api/brand/generate         (L3344) — GPU job: brand_content.py +
                                               ComfyUI + compositor
    GET  /api/brand/campaigns        (L3393) — list generated campaigns
    GET  /api/brand/wordmark         (L3421) — wordmark state
    POST /api/brand/wordmark/approve (L3430) — mark approved
    POST /api/brand/wordmark/upload  (L3438) — install official logo
  Studio (3, the older ComfyUI-studio UI):
    GET  /api/studio/brand           (L3570) — serve brand-kit JSON
    POST /api/studio/upload          (L3579) — upload an image
    POST /api/studio/run             (L3592) — run comfyui_studio.py (4 modes),
                                               SYNCHRONOUS (up to 40-min timeout)

NO WORKER THREADS — brand/generate spawns ``runner.run`` (a GPU job, args
baked in request context); studio/run is synchronous. So the B9 trap
can't recur. brand/copy is an inline one-shot (BRAND_COPY_PROMPT) with a
3-try compliance loop (faithful to the monolith; run_oneshot deferred).

ENGINE + DATA (moved to the new side):
  - brand_content.py (+ compositor.py) — already in backend-app/engines/
    (E1). Referenced by path under the CV venv.
  - brand_templates/ — copied to backend-app/brand_templates/ (S5).
    register_brand derives BRAND_TEMPLATES from the backend-app dir.

CONFIG:
  - COMFY_URL — wired via config.json in app.py (default 127.0.0.1:8188),
    same pattern as BASH (a machine value not derivable from AUTOVSL_ROOT).
  - BRAND_KIT_PATH / BRAND_OUT / COMFY_SCRIPTS / STUDIO_* — derive from
    AUTOVSL_ROOT (register_brand / route call-sites).

DEPENDENCIES:
  - services.helpers.brand:  load_brand_kit, brand_compliance_errors
  - services.llm:            inspiration_block (brand/copy bank refs)
  - services.prompts:        BRAND_COPY_PROMPT
  - services.jobs:           jobs (brand/generate job record)
  - app.config:              AUTOVSL_ROOT, CV_VENV_PY, CLAUDE_RUNNER,
                             JOB_RUNNER, COMFY_URL, BRAND_TEMPLATES,
                             BRAND_CONTENT_PY

MEDIA/PAGE routes NOT here (deferred B2): /brand-out/<rel> (campaign image
server), /brand-studio (page), /studio-out/<name> (studio image server).
studio/run + campaigns return /studio-out//brand-out URLs the B2 media
routes will serve.

server.py is unchanged (Rule 16). Rule 10: engines untouched.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request as _urlreq
import uuid
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_file
from werkzeug.utils import secure_filename

from services.helpers.brand import brand_compliance_errors, load_brand_kit
from services.jobs import jobs
from services.llm import inspiration_block
from services.prompts import BRAND_COPY_PROMPT


brand_bp = Blueprint("brand", __name__)


# ---------------------------------------------------------------- brand routes

@brand_bp.get("/api/brand/health")
def api_brand_health():
    """Plain-language preflight: ComfyUI reachable? fonts present? wordmark ready?
    Pure relocation of server.py L3238-3265."""
    kit_path = current_app.config["BRAND_KIT_PATH"]
    kit = load_brand_kit(kit_path)
    comfy_host = current_app.config["COMFY_URL"]
    checks = []
    comfy_ok = False
    try:
        with _urlreq.urlopen(f"http://{comfy_host}/system_stats", timeout=4) as r:
            comfy_ok = r.status == 200
    except Exception:
        comfy_ok = False
    checks.append({"name": "ComfyUI image engine", "ok": comfy_ok,
                   "fix": None if comfy_ok else "Start ComfyUI: run C:\\ComfyUI_windows_portable\\run_nvidia_lowvram.bat, then retry."})
    fonts = kit.get("fonts", {})
    font_missing = [role for role, s in fonts.items()
                    if not (kit_path.parent / s.get("file", "")).is_file()]
    checks.append({"name": "Brand fonts", "ok": not font_missing,
                   "fix": None if not font_missing else f"Missing font files: {font_missing}"})
    wm = kit.get("wordmark", {})
    wm_file = wm.get("user_override") or wm.get("gold_on_dark", "")
    wm_ok = bool(wm_file) and (kit_path.parent / wm_file).is_file()
    checks.append({"name": "liitt wordmark", "ok": wm_ok, "approved": wm.get("approved", False),
                   "fix": None if wm_ok else "Wordmark PNG not found — regenerate it."})
    healthy = all(c["ok"] for c in checks)
    return jsonify({"healthy": healthy, "checks": checks, "brand": kit.get("brand", "")})


@brand_bp.get("/api/brand/formats")
def api_brand_formats():
    """List brand templates + platforms/presets. Pure relocation of server.py L3268-3283."""
    kit = load_brand_kit(current_app.config["BRAND_KIT_PATH"])
    templates_dir = current_app.config["BRAND_TEMPLATES"]
    formats = []
    if templates_dir.is_dir():
        for f in sorted(templates_dir.glob("*.json")):
            try:
                t = json.loads(f.read_text(encoding="utf-8"))
                formats.append({"template": f.stem, "id": t.get("id"), "label": t.get("label"),
                                "platform": t.get("platform"), "default_preset": t.get("default_preset")})
            except Exception:
                pass
    return jsonify({"formats": formats,
                    "platforms": kit.get("platforms", {}),
                    "presets": [{"id": p["id"], "label": p.get("label")} for p in kit.get("presets", [])],
                    "wordmark_approved": kit.get("wordmark", {}).get("approved", False)})


@brand_bp.post("/api/brand/copy")
def api_brand_copy():
    """Generate structured, compliant on-image copy via the local Claude CLI.
    Pure relocation of server.py L3286-3341. Inline one-shot with a 3-attempt
    compliance-retry loop."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    kit = load_brand_kit(current_app.config["BRAND_KIT_PATH"])
    runner = current_app.config["CLAUDE_RUNNER"]
    if not runner.claude_exe:
        abort(500, "local Claude CLI not found — copy generation needs it")
    body = request.get_json(force=True)
    tpl_name = Path(body.get("template") or "").name
    tpl_path = current_app.config["BRAND_TEMPLATES"] / f"{tpl_name}.json"
    if not tpl_path.is_file():
        abort(404, "unknown template")
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    brief = (body.get("brief") or tpl.get("copy_brief") or "").strip()

    prod = kit.get("product", {})
    comp = kit.get("compliance", {})
    refs = []
    for hid in (body.get("hooks") or []):
        refs.append({"type": "hook", "id": hid})
    for aid in (body.get("angles") or []):
        refs.append({"type": "angle", "id": aid})
    insp = inspiration_block(autovsl, refs) if refs else ""

    prompt = BRAND_COPY_PROMPT.format(
        brand_name=kit.get("brand", "liitt"), brand=prod.get("brand", "liitt"),
        product=prod.get("name", "Fairy Flame"), actives=prod.get("actives_phrase", "microdose gummies"),
        offer=", ".join(f"{k} {v}" for k, v in prod.get("prices", {}).items()) or "see site",
        banned=", ".join(comp.get("banned_words", [])), brief=brief, inspiration=insp)

    env = current_app.config["JOB_RUNNER"]._job_env_factory()
    env.pop("CLAUDECODE", None)
    last_err = ""
    for attempt in range(3):
        try:
            r = subprocess.run([runner.claude_exe, "-p", "--model", "sonnet"], input=prompt,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=180, cwd=str(autovsl), env=env)
        except subprocess.TimeoutExpired:
            abort(504, "copywriter took too long — try again")
        out = r.stdout or ""
        i, j2 = out.find("{"), out.rfind("}")
        if i < 0 or j2 <= i:
            last_err = "no JSON returned"
            continue
        try:
            copy = json.loads(out[i:j2 + 1])
        except json.JSONDecodeError:
            last_err = "invalid JSON"
            continue
        errs = brand_compliance_errors(kit, copy)
        if errs:
            last_err = "; ".join(errs)
            prompt = prompt + f"\n\nYour previous attempt violated compliance ({last_err}). Rewrite, fixing it."
            continue
        return jsonify({"copy": copy, "compliant": True})
    abort(502, f"could not get compliant copy after 3 tries: {last_err}")


@brand_bp.post("/api/brand/generate")
def api_brand_generate():
    """Run the full brief→imagery→composite pipeline as a background GPU job.
    Pure relocation of server.py L3344-3390."""
    autovsl = current_app.config["AUTOVSL_ROOT"]
    kit_path = current_app.config["BRAND_KIT_PATH"]
    kit = load_brand_kit(kit_path)
    body = request.get_json(force=True)
    tpl_name = Path(body.get("template") or "").name
    tpl_path = current_app.config["BRAND_TEMPLATES"] / f"{tpl_name}.json"
    if not tpl_path.is_file():
        abort(404, "unknown template")
    tpl = json.loads(tpl_path.read_text(encoding="utf-8"))
    copy = body.get("copy")
    if not isinstance(copy, dict) or not copy.get("headline"):
        abort(400, "need generated copy (call /api/brand/copy first)")
    errs = brand_compliance_errors(kit, copy)
    if errs:
        abort(400, "copy fails compliance: " + "; ".join(errs))
    platform = tpl.get("platform")
    if platform not in kit.get("platforms", {}):
        abort(400, "template platform not in brand kit")

    brand_out = current_app.config["BRAND_OUT"]
    campaign = secure_filename(body.get("campaign") or f"camp-{time.strftime('%Y%m%d-%H%M%S')}")
    brand_out.mkdir(parents=True, exist_ok=True)
    (brand_out / campaign).mkdir(exist_ok=True)
    content_file = brand_out / campaign / "_copy.json"
    content_file.write_text(json.dumps(copy, indent=1), encoding="utf-8")

    cmd = [str(Path(current_app.config["CV_VENV_PY"])), str(current_app.config["BRAND_CONTENT_PY"]),
           "--kit", str(kit_path), "--platform", platform,
           "--template", str(tpl_path), "--content", str(content_file),
           "--campaign", campaign, "--out-dir", str(brand_out),
           "--scripts-dir", str(autovsl / "scripts"), "--comfy-url", current_app.config["COMFY_URL"],
           "--seed", str(int(body.get("seed") or 1))]
    if body.get("preset"):
        cmd += ["--preset", str(body["preset"])]
    if body.get("bg_prompt"):
        cmd += ["--bg-prompt", str(body["bg_prompt"])]

    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "action": "brand-content", "slug": campaign,
        "label": f"Brand ad ({tpl.get('label', tpl_name)}) — {campaign}",
        "status": "running", "lines": [], "returncode": None,
        "started": time.time(), "ended": None,
        "gpu": True,   # ComfyUI generate + upscale hold the GPU
    }
    runner = current_app.config["JOB_RUNNER"]
    threading.Thread(target=runner.run, args=(job_id, cmd), daemon=True).start()
    return jsonify({"job_id": job_id, "campaign": campaign})


@brand_bp.get("/api/brand/campaigns")
def api_brand_campaigns():
    """List generated campaigns + their images. Pure relocation of server.py L3393-3407."""
    brand_out = current_app.config["BRAND_OUT"]
    camps = []
    if brand_out.is_dir():
        for d in sorted(brand_out.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            imgs = sorted([p for p in d.glob("*.jpg")] + [p for p in d.glob("*.png")],
                          key=lambda p: p.stat().st_mtime, reverse=True)
            imgs = [p for p in imgs if not p.name.startswith(("background-", "_"))]
            if not imgs:
                continue
            camps.append({"campaign": d.name, "mtime": d.stat().st_mtime,
                          "images": [f"/brand-out/{d.name}/{p.name}" for p in imgs]})
    return jsonify({"campaigns": camps})


@brand_bp.get("/api/brand/wordmark")
def api_brand_wordmark():
    """Wordmark state. Pure relocation of server.py L3421-3427."""
    kit = load_brand_kit(current_app.config["BRAND_KIT_PATH"])
    wm = kit.get("wordmark", {})
    rel = wm.get("gold_on_dark", "")
    return jsonify({"img": f"/media/banks/{rel}" if rel else None,
                    "approved": wm.get("approved", False)})


@brand_bp.post("/api/brand/wordmark/approve")
def api_brand_wordmark_approve():
    """Mark the wordmark approved. Pure relocation of server.py L3430-3435."""
    kit_path = current_app.config["BRAND_KIT_PATH"]
    kit = load_brand_kit(kit_path)
    kit.setdefault("wordmark", {})["approved"] = True
    kit_path.write_text(json.dumps(kit, indent=2), encoding="utf-8")
    return jsonify({"approved": True})


@brand_bp.post("/api/brand/wordmark/upload")
def api_brand_wordmark_upload():
    """Install the OFFICIAL logo as the locked wordmark override.
    Pure relocation of server.py L3438-3456."""
    kit_path = current_app.config["BRAND_KIT_PATH"]
    f = request.files.get("logo")
    if not f or not f.filename:
        abort(400, "no file")
    if Path(f.filename).suffix.lower() not in (".png", ".webp"):
        abort(400, "PNG (transparent background) expected")
    dest_dir = kit_path.parent / "brand-assets" / "wordmark"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "official-logo.png"
    f.save(dest)
    kit = load_brand_kit(kit_path)
    wm = kit.setdefault("wordmark", {})
    wm["user_override"] = "brand-assets/wordmark/official-logo.png"
    wm["approved"] = True
    kit_path.write_text(json.dumps(kit, indent=2), encoding="utf-8")
    return jsonify({"installed": True, "path": str(dest)})


# ---------------------------------------------------------------- studio routes

@brand_bp.get("/api/studio/brand")
def studio_brand():
    """liitt / Fairy Flame brand kit as generation presets. Pure relocation
    of server.py L3570-3576."""
    f = current_app.config["AUTOVSL_ROOT"] / "banks" / "liitt-brand-kit.json"
    if not f.is_file():
        return jsonify({"presets": [], "style_suffix": "", "negative_prompt": ""})
    return send_file(str(f), mimetype="application/json")


@brand_bp.post("/api/studio/upload")
def studio_upload():
    """Upload an image into the studio inbox. Pure relocation of server.py L3579-3589."""
    studio_in = current_app.config["AUTOVSL_ROOT"] / "output" / "comfyui-studio" / "_in"
    f = request.files.get("image")
    if not f:
        abort(400, "no image uploaded")
    studio_in.mkdir(parents=True, exist_ok=True)
    ext = Path(f.filename or "img.png").suffix.lower() or ".png"
    dest = studio_in / f"up_{uuid.uuid4().hex[:8]}{ext}"
    f.save(str(dest))
    return jsonify({"path": str(dest)})


@brand_bp.post("/api/studio/run")
def studio_run():
    """Run comfyui_studio.py (generate | upscale | inpaint | keyframe).
    Pure relocation of server.py L3592-3638. SYNCHRONOUS (up to 40-min
    timeout) — faithful to the monolith; uses its own PYTHONUTF8 env
    (not the job-env factory)."""
    import os
    autovsl = current_app.config["AUTOVSL_ROOT"]
    studio_out = autovsl / "output" / "comfyui-studio"
    b = request.get_json(force=True) or {}
    mode = b.get("mode")
    if mode not in ("generate", "upscale", "inpaint", "keyframe"):
        abort(400, "bad mode")
    venv_py = Path(current_app.config["CV_VENV_PY"])
    script = autovsl / "scripts" / "comfyui_studio.py"
    cmd = [str(venv_py), str(script), mode]
    if mode == "generate":
        cmd += [b.get("prompt", ""),
                "--count", str(int(b.get("count", 4))),
                "--width", str(int(b.get("width", 512))),
                "--height", str(int(b.get("height", 768)))]
        if b.get("negative"):
            cmd += ["--negative", b["negative"]]
    elif mode == "upscale":
        cmd += [b.get("image", "")]
    elif mode == "inpaint":
        cmd += [b.get("image", ""), b.get("prompt", "")]
        if b.get("mask"):
            cmd += ["--mask", b["mask"]]
    elif mode == "keyframe":
        cmd += [b.get("image", ""), b.get("prompt", ""),
                "--width", str(int(b.get("width", 512))),
                "--height", str(int(b.get("height", 768))),
                "--strength", str(float(b.get("strength", 0.8)))]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        r = subprocess.run(cmd, cwd=str(autovsl), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=2400)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "results": [], "log": "timed out (>40 min)"})
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    results = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("→"):   # the "→ <path>" result lines
            p = line[1:].strip()
            try:
                rel = str(Path(p).resolve().relative_to(studio_out.resolve()))
                results.append("/studio-out/" + rel.replace("\\", "/"))
            except Exception:
                pass
    return jsonify({"ok": bool(r.returncode == 0 and results),
                    "results": results, "log": out[-3000:]})


# ---------------------------------------------------------------- wire-up

def register_brand(app) -> None:
    """Register the Brand Studio Blueprint and stash its paths.

    Config keys:
      - ``BRAND_TEMPLATES`` — backend-app/brand_templates/ (copied in S5;
        derived from the backend-app dir, like dubsync's engine dir).
      - ``BRAND_CONTENT_PY`` — backend-app/engines/brand_content.py (E1).
      - ``BRAND_KIT_PATH`` — autovsl/banks/liitt-brand-kit.json.
      - ``BRAND_OUT`` — autovsl/output/brand-content.
      - ``COMFY_URL`` — from app.py (config.json; default 127.0.0.1:8188).
    CV venv / CLAUDE_RUNNER / JOB_RUNNER come from earlier wiring.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    autovsl = Path(app.config["AUTOVSL_ROOT"])
    app.config["BRAND_TEMPLATES"] = backend_dir / "brand_templates"
    app.config["BRAND_CONTENT_PY"] = str(backend_dir / "engines" / "brand_content.py")
    app.config["BRAND_KIT_PATH"] = autovsl / "banks" / "liitt-brand-kit.json"
    app.config["BRAND_OUT"] = autovsl / "output" / "brand-content"
    app.register_blueprint(brand_bp)
